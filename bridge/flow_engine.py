from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Awaitable, Callable

import aiohttp
import paho.mqtt.client as mqtt

from .notifier import Notifier
from .scheduling import is_within_window

logger = logging.getLogger("bridge.flow_engine")

_LEVEL_PRIORITY = {"info": "default", "warning": "high", "critical": "urgent"}
_LEVEL_EMOJI = {"info": "ℹ️", "warning": "⚠️", "critical": "🔴"}

_TRIGGER_TYPES = ("trigger", "mqttIn")


def _num(value: object) -> float | object:
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


_OPS: dict[str, Callable[[object, object], bool]] = {
    ">": lambda a, b: _num(a) > _num(b),
    "<": lambda a, b: _num(a) < _num(b),
    ">=": lambda a, b: _num(a) >= _num(b),
    "<=": lambda a, b: _num(a) <= _num(b),
    "==": lambda a, b: str(a) == str(b) or _num(a) == _num(b),
    "!=": lambda a, b: str(a) != str(b) and _num(a) != _num(b),
}


def _resolve_template(value: object, msg: dict) -> object:
    if value == "{value}":
        return msg.get("payload")
    if isinstance(value, str) and ("{value}" in value or "{key}" in value):
        return value.replace("{value}", str(msg.get("payload", ""))).replace("{key}", str(msg.get("key", "")))
    return value


class FlowEngine:
    """Edytor "Flow" - grafy węzłów w stylu Node-RED, jako alternatywa dla reguł
    ruchu dla bardziej złożonych automatyzacji (wiele kroków, warunki gałęziowe,
    MQTT/HTTP). Nie zastępuje jeszcze reguł ruchu (`RulesEngine.handle_motion`/
    `handle_device_state`) - działa obok, nasłuchując tych samych zdarzeń.

    Flow: {id, name, enabled, nodes: [Node]}
    Node: {id, type, config: {...}, wires: [[target_id, ...], ...]}
      wires[port] = lista id węzłów podłączonych do danego wyjścia węzła.

    Typy węzłów:
      trigger   {source: satel_zone|tuya_state|homewhiz_state, zone_number?,
                 source_device?, trigger_value?, op: violated|restored|changes}
                wejście grafu - odpalane przez handle_motion/handle_device_state,
                dokładnie te same zdarzenia co reguły ruchu.
      condition {op: > < >= <= == !=, value}  - [dopasowano, else]
      device    {target_device, target_system: tuya|satel_output,
                 value: "on"|"off", dimmer_level_percent?}
      scene     {scene_name}  - uruchamia istniejącą, nazwaną scenę
      delay     {seconds}
      notify    {level: info|warning|critical, message}  - {value}/{key} w
                treści podstawiają wartość/klucz z wiadomości
      mqttIn    {topic}  - wejście grafu, odpalane wiadomością MQTT
      mqttOut   {topic, payload}  - {value}/{key} podstawiane jak w notify
      http      {method, url, body}  - odpowiedź trafia do payload wiadomości
      schedule  {start, end}  - jak time_window reguły ruchu (w tym sunset/
                sunrise+-N) - przepuszcza wiadomość TYLKO w oknie czasowym,
                poza nim ją wygasza (bez propagacji dalej)
      escalation {threshold_count, window_seconds}  - [normalnie, eskalacja]:
                liczy wejścia w ruchomym oknie czasowym, po przekroczeniu
                progu przepuszcza wyjściem "eskalacja" (i zeruje licznik)
                zamiast "normalnie" - analogicznie do `escalation` reguły
                ruchu (RulesEngine._fire_motion_rule)
      state     {key, op: equals|not_equals, value}  - [spełniony, inaczej]:
                porównuje DOWOLNY klucz statusu widoczny na panelu głównym
                (np. "satel_input:3", "tuya:gniazdko_salon" - ten sam klucz
                co warunek "state" reguły ruchu) z zadaną wartością, NIE
                payload wiadomości (w odróżnieniu od węzła "condition")
    """

    def __init__(
        self,
        flows_cfg: list[dict],
        *,
        set_target_state: Callable[[str, str, bool, int | None], Awaitable[None]],
        run_scene: Callable[[str], Awaitable[None]],
        notify_cfg: dict | None,
        mqtt_cfg: dict | None = None,
        location_cfg: dict | None = None,
        get_status_value: Callable[[str], str | None] | None = None,
    ):
        self.flows = flows_cfg or []
        self._set_target_state = set_target_state
        self._run_scene = run_scene
        self._notifier = Notifier(notify_cfg)
        self._mqtt_cfg = mqtt_cfg or {}
        self._mqtt_client: mqtt.Client | None = None
        self._mqtt_subs: dict[str, list[tuple[dict, dict]]] = {}
        self._mqtt_topics: set[str] = set()
        self._trigger_state: dict[str, bool] = {}
        self._get_status_value = get_status_value or (lambda key: None)
        self._escalation_history: dict[str, list[float]] = {}
        self.location_cfg = location_cfg
        self._loop = asyncio.get_event_loop()

    def start(self) -> None:
        self._setup_mqtt_triggers()

    def stop(self) -> None:
        if self._mqtt_client is not None:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            self._mqtt_client = None

    def update_flows(self, flows_cfg: list[dict]) -> None:
        self.flows = flows_cfg or []
        self._trigger_state.clear()
        self._escalation_history.clear()
        self._setup_mqtt_triggers()

    # ── Wejścia zdarzeń (te same co RulesEngine.handle_motion/handle_device_state) ──

    async def handle_motion(self, source: str, zone_or_device_id, trigger: str = "violated") -> None:
        for flow in self.flows:
            if not flow.get("enabled", True):
                continue
            for node in flow.get("nodes", []):
                if node.get("type") != "trigger":
                    continue
                cfg = node.get("config") or {}
                if cfg.get("source") != source:
                    continue
                if source == "satel_zone" and cfg.get("zone_number") != zone_or_device_id:
                    continue
                op = cfg.get("op", "violated")
                if op != "changes" and op != trigger:
                    continue
                msg = {
                    "payload": trigger == "violated", "key": zone_or_device_id,
                    "source": f"flow:{flow.get('name')}",
                }
                asyncio.create_task(self._propagate(flow, node, msg, 0))

    async def handle_device_state(self, source: str, device_name: str, value: str) -> None:
        for flow in self.flows:
            if not flow.get("enabled", True):
                continue
            for node in flow.get("nodes", []):
                if node.get("type") != "trigger":
                    continue
                cfg = node.get("config") or {}
                if cfg.get("source") != source or cfg.get("source_device") != device_name:
                    continue
                op = cfg.get("op", "changes")
                state_key = f"{flow.get('id')}:{node.get('id')}"
                if op == "changes":
                    fire = True
                else:
                    matched = value == cfg.get("trigger_value")
                    fire = matched and not self._trigger_state.get(state_key, False)
                    self._trigger_state[state_key] = matched
                if not fire:
                    continue
                msg = {"payload": value, "key": device_name, "source": f"flow:{flow.get('name')}"}
                asyncio.create_task(self._propagate(flow, node, msg, 0))

    # ── MQTT (opcjonalne - tylko gdy flow zawiera węzeł mqttIn/mqttOut) ──

    def _setup_mqtt_triggers(self) -> None:
        want_topics: set[str] = set()
        self._mqtt_subs = {}
        for flow in self.flows:
            if not flow.get("enabled", True):
                continue
            for node in flow.get("nodes", []):
                if node.get("type") != "mqttIn":
                    continue
                topic = (node.get("config") or {}).get("topic")
                if not topic:
                    continue
                want_topics.add(topic)
                self._mqtt_subs.setdefault(topic, []).append((flow, node))
        self._sync_mqtt(want_topics)

    def _ensure_mqtt_client(self) -> mqtt.Client | None:
        if self._mqtt_client is not None:
            return self._mqtt_client
        host = self._mqtt_cfg.get("mqtt_host")
        if not host:
            return None
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        client.on_message = self._on_mqtt_message
        client.on_connect = self._on_mqtt_connect
        client.reconnect_delay_set(min_delay=1, max_delay=120)
        try:
            client.connect_async(host, self._mqtt_cfg.get("mqtt_port", 1883))
            client.loop_start()
        except Exception:
            logger.exception("Flow: nie udało się połączyć z brokerem MQTT (%s)", host)
            return None
        self._mqtt_client = client
        return client

    def _sync_mqtt(self, want_topics: set[str]) -> None:
        if not want_topics:
            return
        client = self._ensure_mqtt_client()
        if client is None:
            logger.warning(
                "Flow: obecne są węzły MQTT (mqttIn/mqttOut), ale brak skonfigurowanego brokera "
                "(zakładka 🧩 Wtyczki -> Publikacja stanu do MQTT) - te węzły nie zadziałają"
            )
            return
        for topic in want_topics - self._mqtt_topics:
            client.subscribe(topic)
        self._mqtt_topics = want_topics

    def _on_mqtt_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        for topic in self._mqtt_topics:
            client.subscribe(topic)

    def _on_mqtt_message(self, client, userdata, msg) -> None:
        payload = msg.payload.decode(errors="replace")
        for flow, node in self._mqtt_subs.get(msg.topic, []):
            fmsg = {"payload": payload, "key": msg.topic, "source": f"flow:{flow.get('name')}"}
            asyncio.run_coroutine_threadsafe(self._propagate(flow, node, fmsg, 0), self._loop)

    # ── Wykonanie grafu ──

    async def _propagate(self, flow: dict, node: dict, msg: dict, depth: int) -> None:
        if depth > 50:
            logger.warning(
                "Flow '%s': przekroczono limit głębokości (50) - możliwa pętla, przerywam", flow.get("name"),
            )
            return
        try:
            outputs = await self._exec_node(flow, node, msg)
        except Exception as exc:
            logger.error(
                "Flow '%s': węzeł %s (%s) nie powiódł się: %s", flow.get("name"), node.get("id"), node.get("type"), exc,
            )
            await self._notifier.send(
                "🔴 Flow błąd", f"Flow \"{flow.get('name')}\": węzeł {node.get('type')} nie powiódł się - {exc}",
                priority="high",
            )
            return

        wires = node.get("wires") or []
        nodes_by_id = {n.get("id"): n for n in flow.get("nodes", [])}
        for port, out_msg in enumerate(outputs):
            if out_msg is None:
                continue
            for target_id in (wires[port] if port < len(wires) else []):
                target = nodes_by_id.get(target_id)
                if target is not None:
                    asyncio.create_task(self._propagate(flow, target, dict(out_msg), depth + 1))

    async def _exec_node(self, flow: dict, node: dict, msg: dict) -> list[dict | None]:
        c = node.get("config") or {}
        ntype = node.get("type")

        if ntype in _TRIGGER_TYPES:
            return [msg]

        if ntype == "condition":
            cmp = _OPS.get(c.get("op"))
            passed = bool(cmp(msg.get("payload"), c.get("value"))) if cmp else False
            return [msg if passed else None, None if passed else msg]

        if ntype == "schedule":
            window = {"start": c.get("start"), "end": c.get("end")}
            if is_within_window(window, self.location_cfg, dt.datetime.now()):
                return [msg]
            return [None]

        if ntype == "state":
            actual = self._get_status_value(c.get("key"))
            passed = actual != c.get("value") if c.get("op") == "not_equals" else actual == c.get("value")
            return [msg if passed else None, None if passed else msg]

        if ntype == "escalation":
            key = f"{flow.get('id')}:{node.get('id')}"
            history = self._escalation_history.setdefault(key, [])
            now = time.time()
            history.append(now)
            cutoff = now - float(c.get("window_seconds") or 0)
            history[:] = [t for t in history if t >= cutoff]
            threshold = int(c.get("threshold_count") or 1)
            if len(history) >= threshold:
                history.clear()
                return [None, msg]
            return [msg, None]

        if ntype == "device":
            device = c.get("target_device")
            target_system = c.get("target_system", "tuya")
            on = str(c.get("value", "on")).lower() not in ("off", "false", "0")
            dimmer = c.get("dimmer_level_percent")
            await self._set_target_state(target_system, device, on, dimmer)
            return [msg]

        if ntype == "scene":
            scene_name = c.get("scene_name")
            if scene_name:
                await self._run_scene(scene_name)
            return [msg]

        if ntype == "delay":
            await asyncio.sleep(float(c.get("seconds") or 0))
            return [msg]

        if ntype == "notify":
            level = c.get("level") or "info"
            text = str(_resolve_template(c.get("message") or "", msg))
            await self._notifier.send(
                f"{_LEVEL_EMOJI.get(level, 'ℹ️')} Flow: {flow.get('name')}", text,
                priority=_LEVEL_PRIORITY.get(level, "default"),
            )
            return [msg]

        if ntype == "mqttOut":
            client = self._ensure_mqtt_client()
            topic = c.get("topic")
            if client is not None and topic:
                payload = _resolve_template(c.get("payload") if c.get("payload") not in (None, "") else "{value}", msg)
                client.publish(topic, payload=str(payload))
            return [msg]

        if ntype == "http":
            method = (c.get("method") or "GET").upper()
            url = c.get("url")
            body = _resolve_template(c.get("body"), msg) if method != "GET" else None
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method, url, data=str(body) if body is not None else None,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        text = await resp.text()
            except Exception as exc:
                raise RuntimeError(f"HTTP {method} {url}: {exc}") from exc
            return [{**msg, "payload": text}]

        return [msg]

    async def run_flow(self, flow_id: str) -> None:
        """Ręczne uruchomienie flow (przycisk "Testuj" w edytorze) - odpala
        wszystkie jego węzły wejściowe (trigger/mqttIn) z payloadem None."""
        flow = next((f for f in self.flows if f.get("id") == flow_id), None)
        if flow is None:
            raise ValueError(f"Nieznane flow: {flow_id!r}")
        for node in flow.get("nodes", []):
            if node.get("type") in _TRIGGER_TYPES:
                msg = {"payload": None, "key": None, "source": f"flow-test:{flow.get('name')}"}
                await self._propagate(flow, node, msg, 0)
