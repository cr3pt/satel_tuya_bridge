from __future__ import annotations

import copy
import json
import pathlib
import uuid

from werkzeug.security import generate_password_hash

import plugins

DRAFT_FILENAME = ".setup_draft.json"

SCENE_ACTION_REQUIRED_FIELDS = {
    "tuya_on": ["device"],
    "tuya_off": ["device"],
    "tuya_on_for_duration": ["device", "duration_seconds"],
    "satel_output": ["output_number"],
    "satel_virtual_input": ["virtual_input"],
    "satel_arm": [],
    "satel_disarm": [],
    "delay": ["seconds"],
}


def validate(form: dict) -> list[str]:
    errors = []

    satel = form.get("satel", {})
    if not satel.get("host"):
        errors.append("Satel: adres IP centrali/modułu ETHM jest wymagany")

    ebus = form.get("ebus", {})
    if not ebus.get("mqtt_host"):
        errors.append("eBUS: adres brokera MQTT jest wymagany")

    username = (form.get("admin_username") or "").strip()
    if not username:
        errors.append("Nazwa użytkownika administratora jest wymagana")

    password = form.get("admin_password", "")
    if len(password) < 8:
        errors.append("Hasło panelu: minimum 8 znaków")

    errors.extend(validate_satel_fields(form))
    errors.extend(validate_tuya_devices(form))
    errors.extend(validate_cloud_devices(form))
    errors.extend(validate_automations(form))

    return errors


def validate_satel_fields(form: dict) -> list[str]:
    errors = []
    for i, vi in enumerate(form.get("virtual_inputs", [])):
        if not vi.get("name") or not vi.get("number"):
            errors.append(f"Wirtualne wejście #{i + 1}: nazwa i numer wejścia są wymagane")
    return errors


def validate_tuya_devices(form: dict) -> list[str]:
    errors = []
    gateway_names = {
        g["name"].strip() for g in form.get("zigbee_gateways", [])
        if g.get("name") and g.get("id") and g.get("ip") and g.get("local_key")
    }
    for i, device in enumerate(form.get("tuya_devices", [])):
        label = f"Urządzenie Tuya #{i + 1}"
        gateway_name = (device.get("gateway") or "").strip()
        if gateway_name:
            if not device.get("name") or not device.get("id"):
                errors.append(f"{label} (podurządzenie Zigbee): nazwa i ID są wymagane")
            if gateway_name not in gateway_names:
                errors.append(
                    f"{label}: wskazana bramka Zigbee '{gateway_name}' nie istnieje wśród "
                    "zdefiniowanych bramek (sprawdź nazwę albo uzupełnij bramkę niżej)"
                )
        elif not device.get("name") or not device.get("id") or not device.get("ip") or not device.get("local_key"):
            errors.append(f"{label}: nazwa, ID, adres IP i local_key są wymagane")

    for i, gateway in enumerate(form.get("zigbee_gateways", [])):
        if not gateway.get("name") or not gateway.get("id") or not gateway.get("ip") or not gateway.get("local_key"):
            errors.append(f"Bramka Zigbee #{i + 1}: nazwa, ID, adres IP i local_key są wymagane")
    return errors


def validate_cloud_devices(form: dict) -> list[str]:
    errors = []
    for i, device in enumerate(form.get("cloud_devices", [])):
        label = f"Urządzenie Tuya Cloud #{i + 1}"
        if not device.get("name") or not device.get("id"):
            errors.append(f"{label}: nazwa i ID są wymagane")
    return errors


def validate_automations(form: dict) -> list[str]:
    scene_names = {s.get("name", "").strip() for s in form.get("scenes", []) if s.get("name", "").strip()}
    rule_names = {r.get("name", "").strip() for r in form.get("motion_rules", []) if r.get("name", "").strip()}
    errors = []
    errors.extend(_validate_scenes(form.get("scenes", [])))
    errors.extend(_validate_motion_rules(form.get("motion_rules", []), scene_names))
    errors.extend(_validate_presence_simulation(form.get("presence_simulation", {})))
    errors.extend(validate_flows(form.get("flows", []), scene_names))
    collision = scene_names & rule_names
    if collision:
        errors.append(
            f"Nazwa(-y) {sorted(collision)} użyta(-e) jednocześnie jako nazwa sceny i reguły ruchu - "
            f"nazwy scen i reguł ruchu muszą być unikalne między sobą (współdzielą jeden klucz statusu "
            f"do warunku 'inna automatyzacja jest uruchomiona')"
        )
    return errors


_FLOW_NODE_TYPES = (
    "trigger", "condition", "device", "scene", "delay", "notify", "mqttIn", "mqttOut", "http",
    "schedule", "escalation", "state", "condition_group", "tuya_dp", "stop",
)
_FLOW_STATE_OPS = ("equals", "not_equals", "gt", "lt", "gte", "lte")
_FLOW_TRIGGER_SOURCES = ("satel_zone", "tuya_state", "homewhiz_state")
_FLOW_CONDITION_OPS = (">", "<", ">=", "<=", "==", "!=")
_FLOW_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _validate_flow_node(node: dict, label: str, scene_names: set[str]) -> list[str]:
    errors = []
    ntype = node.get("type")
    if not node.get("id"):
        errors.append(f"{label}: brak identyfikatora węzła")
    if ntype not in _FLOW_NODE_TYPES:
        errors.append(f"{label}: nieznany typ węzła '{ntype}'")
        return errors

    c = node.get("config") or {}
    if ntype == "trigger":
        source = c.get("source") or "satel_zone"
        if source not in _FLOW_TRIGGER_SOURCES:
            errors.append(f"{label}: nieznane źródło '{source}'")
        elif source == "satel_zone":
            if not c.get("zone_number"):
                errors.append(f"{label}: numer wejścia Satela jest wymagany")
        else:
            if not (c.get("source_device") or "").strip():
                errors.append(f"{label}: wybierz urządzenie źródłowe")
    elif ntype == "condition":
        if c.get("op") not in _FLOW_CONDITION_OPS:
            errors.append(f"{label}: nieznany operator '{c.get('op')}'")
        if c.get("value") in (None, ""):
            errors.append(f"{label}: warunek wymaga wartości porównania")
    elif ntype == "device":
        if not (c.get("target_device") or "").strip():
            errors.append(f"{label}: wybierz urządzenie docelowe")
        target_system = c.get("target_system") or "tuya"
        if target_system not in ("tuya", "satel_output", "satel_virtual_input"):
            errors.append(f"{label}: nieznany system urządzenia docelowego '{target_system}'")
        value = c.get("value") or "on"
        if value not in ("on", "off", "toggle"):
            errors.append(f"{label}: nieznana wartość akcji '{value}' (dozwolone: on/off/toggle)")
    elif ntype == "scene":
        scene_name = (c.get("scene_name") or "").strip()
        if not scene_name:
            errors.append(f"{label}: wybierz scenę")
        elif scene_name not in scene_names:
            errors.append(f"{label}: scena '{scene_name}' nie istnieje wśród zdefiniowanych scen")
    elif ntype == "delay":
        if c.get("seconds") in (None, ""):
            errors.append(f"{label}: opóźnienie wymaga liczby sekund")
    elif ntype == "notify":
        if not (c.get("message") or "").strip():
            errors.append(f"{label}: powiadomienie wymaga treści")
    elif ntype in ("mqttIn", "mqttOut"):
        if not (c.get("topic") or "").strip():
            errors.append(f"{label}: podaj temat MQTT")
    elif ntype == "http":
        if not (c.get("url") or "").strip():
            errors.append(f"{label}: podaj adres URL")
        method = (c.get("method") or "GET").upper()
        if method not in _FLOW_HTTP_METHODS:
            errors.append(f"{label}: nieznana metoda HTTP '{method}'")
    elif ntype == "schedule":
        if not c.get("start") or not c.get("end"):
            errors.append(f"{label}: harmonogram wymaga początku i końca okna czasowego")
    elif ntype == "escalation":
        if not c.get("threshold_count"):
            errors.append(f"{label}: eskalacja wymaga progu (liczby naruszeń)")
        if not c.get("window_seconds"):
            errors.append(f"{label}: eskalacja wymaga okna czasowego (sekundy)")
    elif ntype == "state":
        if not (c.get("key") or "").strip():
            errors.append(f"{label}: warunek stanu wymaga wybrania statusu")
        if c.get("op") not in _FLOW_STATE_OPS:
            errors.append(f"{label}: nieznany operator warunku stanu '{c.get('op')}'")
        if (c.get("value") or "") == "":
            errors.append(f"{label}: warunek stanu wymaga wartości porównania")
    elif ntype == "condition_group":
        errors.extend(_validate_condition(c.get("condition"), f"{label}: warunek"))
    elif ntype == "tuya_dp":
        if not c.get("target_device"):
            errors.append(f"{label}: wybierz urządzenie Tuya")
        if not (c.get("dp_code") or "").strip():
            errors.append(f"{label}: podaj kod pola (DP) do ustawienia")
        if (c.get("value") or "") == "":
            errors.append(f"{label}: podaj wartość do ustawienia")
    return errors


def validate_flows(flows: list[dict], scene_names: set[str]) -> list[str]:
    errors = []
    for i, flow in enumerate(flows):
        label = f"Flow #{i + 1}"
        name = (flow.get("name") or "").strip()
        if not name:
            errors.append(f"{label}: nazwa jest wymagana")

        nodes = flow.get("nodes") or []
        if not nodes:
            errors.append(f"{label} ('{name or '?'}'): musi mieć co najmniej jeden węzeł")

        node_ids = [n.get("id") for n in nodes if n.get("id")]
        if len(set(node_ids)) != len(node_ids):
            errors.append(f"{label} ('{name or '?'}'): identyfikatory węzłów muszą być unikalne")

        for j, node in enumerate(nodes):
            node_label = f"{label} ('{name or '?'}'), węzeł #{j + 1}"
            errors.extend(_validate_flow_node(node, node_label, scene_names))

        if nodes and not any(n.get("type") in ("trigger", "mqttIn") for n in nodes):
            errors.append(
                f"{label} ('{name or '?'}'): potrzebuje co najmniej jednego węzła wejściowego (trigger/mqttIn)"
            )
    return errors


def _validate_scenes(scenes: list[dict]) -> list[str]:
    errors = []
    seen_names = set()
    for i, scene in enumerate(scenes):
        label = f"Scena #{i + 1}"
        name = (scene.get("name") or "").strip()
        if not name:
            errors.append(f"{label}: nazwa jest wymagana")
        elif name in seen_names:
            errors.append(f"{label}: nazwa '{name}' powtarza się - nazwy scen muszą być unikalne")
        else:
            seen_names.add(name)

        actions = scene.get("actions", [])
        if not actions:
            errors.append(f"{label} ('{name or '?'}'): musi mieć co najmniej jedną akcję")
        for j, action in enumerate(actions):
            action_label = f"{label} ('{name or '?'}'), akcja #{j + 1}"
            action_type = action.get("type")
            if action_type not in SCENE_ACTION_REQUIRED_FIELDS:
                errors.append(f"{action_label}: nieznany typ akcji '{action_type}'")
                continue
            for field in SCENE_ACTION_REQUIRED_FIELDS[action_type]:
                if action.get(field) in (None, ""):
                    errors.append(f"{action_label} ({action_type}): pole '{field}' jest wymagane")
    return errors


_CONDITION_COMPOSITE_TYPES = ("and", "or", "not")
_CONDITION_LEAF_TYPES = ("state", "time_window")
_STATE_CONDITION_COMPARISON_KEYS = ("equals", "not_equals", "gt", "lt", "gte", "lte")


def _validate_condition(condition: dict | None, label: str, depth: int = 0) -> list[str]:
    errors: list[str] = []
    if not condition:
        return errors
    if depth > 10:
        errors.append(f"{label}: warunek zagnieżdżony zbyt głęboko (maksymalnie 10 poziomów)")
        return errors

    ctype = condition.get("type")
    if ctype in _CONDITION_COMPOSITE_TYPES:
        sub_conditions = condition.get("conditions") or []
        if not sub_conditions:
            errors.append(f"{label}: warunek '{ctype}' wymaga co najmniej jednego warunku podrzędnego")
        for i, sub in enumerate(sub_conditions):
            errors.extend(_validate_condition(sub, f"{label} -> {ctype}[{i + 1}]", depth + 1))
    elif ctype == "state":
        if not (condition.get("key") or "").strip():
            errors.append(f"{label}: warunek 'state' wymaga wybrania statusu/urządzenia")
        if all(condition.get(k) in (None, "") for k in _STATE_CONDITION_COMPARISON_KEYS):
            errors.append(
                f"{label}: warunek 'state' wymaga wartości porównania "
                f"(równa się / różna od / większe / mniejsze / większe-równe / mniejsze-równe)"
            )
    elif ctype == "time_window":
        if not condition.get("start") or not condition.get("end"):
            errors.append(f"{label}: warunek 'okno czasowe' wymaga początku i końca")
    else:
        errors.append(f"{label}: nieznany typ warunku '{ctype}'")
    return errors


_MOTION_RULE_SOURCES = ("satel_zone", "tuya_state", "homewhiz_state")
_MOTION_RULE_SOURCE_COMBINATORS = ("or", "and")


def _validate_motion_rule_source(source_entry: dict, label: str) -> list[str]:
    errors = []
    source = (source_entry.get("source") or "satel_zone").strip()
    if source not in _MOTION_RULE_SOURCES:
        errors.append(f"{label}: nieznane wejście '{source}'")
    if source == "satel_zone":
        if not source_entry.get("zone_number"):
            errors.append(f"{label}: numer wejścia Satela jest wymagany")
    else:
        if not (source_entry.get("source_device") or "").strip():
            errors.append(f"{label}: wybierz urządzenie źródłowe")
        if not (source_entry.get("trigger_value") or "").strip():
            errors.append(f"{label}: wybierz wartość wyzwalającą")
    return errors


def _validate_motion_rule_action(action_entry: dict, label: str) -> list[str]:
    errors = []
    if "virtual_input" in action_entry:
        return errors
    if not action_entry.get("target_device"):
        errors.append(f"{label}: wybierz urządzenie docelowe")
    target_system = (action_entry.get("target_system") or "tuya").strip()
    if target_system not in ("tuya", "satel_output"):
        errors.append(f"{label}: nieznany system urządzenia docelowego '{target_system}'")
    return errors


def _validate_motion_rules(rules: list[dict], scene_names: set[str]) -> list[str]:
    errors = []
    rule_names = {(r.get("name") or "").strip() for r in rules if (r.get("name") or "").strip()}
    seen_names = set()
    for i, rule in enumerate(rules):
        label = f"Reguła ruchu #{i + 1}"
        sources = rule.get("sources")
        if sources:
            if len(sources) < 2:
                errors.append(f"{label}: lista wielu źródeł ('sources') wymaga co najmniej 2 wpisów")
            combinator = (rule.get("source_combinator") or "or").strip()
            if combinator not in _MOTION_RULE_SOURCE_COMBINATORS:
                errors.append(f"{label}: nieznany łącznik źródeł '{combinator}'")
            for j, source_entry in enumerate(sources):
                errors.extend(_validate_motion_rule_source(source_entry, f"{label}, źródło #{j + 1}"))
            source = (sources[0].get("source") or "satel_zone").strip()
        else:
            errors.extend(_validate_motion_rule_source(rule, label))
            source = (rule.get("source") or "satel_zone").strip()

        name = (rule.get("name") or "").strip()
        if name:
            if name in seen_names:
                errors.append(
                    f"{label}: nazwa '{name}' powtarza się - nazwy reguł muszą być unikalne "
                    f"(potrzebne do wyzwalania łańcuchowego)"
                )
            seen_names.add(name)

        uses_scene = bool((rule.get("scene") or "").strip())
        uses_virtual_input = bool((rule.get("virtual_input") or "").strip())
        actions = rule.get("actions")
        if uses_scene:
            scene_name = rule["scene"].strip()
            if scene_name not in scene_names:
                errors.append(f"{label}: wybrana scena '{scene_name}' nie istnieje wśród zdefiniowanych scen")
        elif actions:
            for j, action_entry in enumerate(actions):
                errors.extend(_validate_motion_rule_action(action_entry, f"{label}, akcja #{j + 1}"))
        elif uses_virtual_input:
            pass
        else:
            if not rule.get("target_device"):
                errors.append(f"{label}: wybierz urządzenie docelowe, wirtualne wejście albo scenę")
            target_system = (rule.get("target_system") or "tuya").strip()
            if target_system not in ("tuya", "satel_output"):
                errors.append(f"{label}: nieznany system urządzenia docelowego '{target_system}'")

        if source == "satel_zone":
            trigger_on = (rule.get("trigger_on") or "violated").strip()
            if trigger_on not in ("violated", "restored"):
                errors.append(f"{label}: nieznany wyzwalacz '{trigger_on}'")

        window = rule.get("time_window") or {}
        if window.get("enabled"):
            if not window.get("start") or not window.get("end"):
                errors.append(f"{label}: harmonogram - podaj początek i koniec okna")

        escalation = rule.get("escalation") or {}
        if escalation.get("enabled"):
            if not escalation.get("threshold_count"):
                errors.append(f"{label}: eskalacja - liczba naruszeń jest wymagana")
            if not escalation.get("window_seconds"):
                errors.append(f"{label}: eskalacja - okno czasowe (sekundy) jest wymagane")
            escalated_scene = (escalation.get("escalated_scene") or "").strip()
            if not escalated_scene:
                errors.append(f"{label}: eskalacja - wybierz scenę wywoływaną po przekroczeniu progu")
            elif escalated_scene not in scene_names:
                errors.append(f"{label}: eskalacja - scena '{escalated_scene}' nie istnieje wśród zdefiniowanych scen")

        errors.extend(_validate_condition(rule.get("condition"), f"{label}: warunek"))

        chain_target = (rule.get("then_trigger_rule") or "").strip()
        if chain_target:
            if chain_target == name:
                errors.append(f"{label}: reguła nie może wyzwalać (then_trigger_rule) samej siebie")
            elif chain_target not in rule_names:
                errors.append(
                    f"{label}: 'wyzwól kolejną regułę' wskazuje na nieistniejącą nazwę reguły '{chain_target}'"
                )
    return errors


def _validate_presence_simulation(ps: dict) -> list[str]:
    errors = []
    if not ps or not ps.get("enabled"):
        return errors

    if not [d for d in ps.get("devices", []) if d]:
        errors.append("Symulacja obecności: wybierz co najmniej jedno urządzenie Tuya")

    window = ps.get("window") or {}
    if window.get("enabled") and not (window.get("start") and window.get("end")):
        errors.append("Symulacja obecności: harmonogram - podaj początek i koniec okna")

    lo, hi = ps.get("min_interval_minutes"), ps.get("max_interval_minutes")
    if lo and hi:
        try:
            if int(lo) > int(hi):
                errors.append("Symulacja obecności: minimalny odstęp nie może być większy niż maksymalny")
        except (TypeError, ValueError):
            errors.append("Symulacja obecności: odstępy muszą być liczbami")
    return errors


def _build_scene_action(action: dict) -> dict:
    action_type = action["type"]
    built = {"type": action_type}
    if action_type in ("tuya_on", "tuya_off", "tuya_on_for_duration"):
        built["device"] = action["device"]
        if action_type != "tuya_off" and action.get("dimmer_level_percent") not in (None, ""):
            built["dimmer_level_percent"] = int(action["dimmer_level_percent"])
        if action_type == "tuya_on_for_duration":
            built["duration_seconds"] = int(action["duration_seconds"])
    elif action_type == "satel_output":
        built["output_number"] = int(action["output_number"])
        built["state"] = bool(action.get("state", True))
    elif action_type == "satel_virtual_input":
        built["virtual_input"] = action["virtual_input"]
    elif action_type == "satel_arm":
        if action.get("mode") not in (None, ""):
            built["mode"] = int(action["mode"])
        if action.get("partitions"):
            built["partitions"] = [int(p) for p in action["partitions"]]
    elif action_type == "satel_disarm":
        if action.get("partitions"):
            built["partitions"] = [int(p) for p in action["partitions"]]
    elif action_type == "delay":
        built["seconds"] = float(action["seconds"])
    return built


def _build_scenes(scenes_form: list[dict]) -> list[dict]:
    built_scenes = []
    for scene in scenes_form:
        if not (scene.get("name") or "").strip():
            continue
        built = {
            "name": scene["name"].strip(),
            "actions": [_build_scene_action(a) for a in scene.get("actions", [])],
        }
        if scene.get("enabled") is False:
            built["enabled"] = False
        built_scenes.append(built)
    return built_scenes


def _build_condition(condition: dict | None, depth: int = 0) -> dict | None:
    if not condition or depth > 10:
        return None
    ctype = condition.get("type")

    if ctype in _CONDITION_COMPOSITE_TYPES:
        sub_conditions = [
            built for built in (
                _build_condition(sub, depth + 1) for sub in condition.get("conditions", [])
            ) if built
        ]
        if not sub_conditions:
            return None
        return {"type": ctype, "conditions": sub_conditions}
    if ctype == "state":
        key = (condition.get("key") or "").strip()
        if not key:
            return None
        built = {"type": "state", "key": key}
        for comp_key in _STATE_CONDITION_COMPARISON_KEYS:
            if condition.get(comp_key) not in (None, ""):
                built[comp_key] = condition[comp_key]
                break
        else:
            return None
        return built
    if ctype == "time_window":
        start, end = condition.get("start"), condition.get("end")
        if not start or not end:
            return None
        return {"type": "time_window", "start": start.strip(), "end": end.strip()}
    return None


def _build_motion_rule_source(entry: dict) -> dict | None:
    source = (entry.get("source") or "satel_zone").strip()
    if source == "satel_zone":
        if not entry.get("zone_number"):
            return None
        return {"source": "satel_zone", "zone_number": int(entry["zone_number"])}
    source_device = (entry.get("source_device") or "").strip()
    trigger_value = (entry.get("trigger_value") or "").strip()
    if not source_device or not trigger_value:
        return None
    return {"source": source, "source_device": source_device, "trigger_value": trigger_value}


def _build_motion_rule_action(entry: dict) -> dict | None:
    virtual_input_name = (entry.get("virtual_input") or "").strip()
    if virtual_input_name:
        return {"virtual_input": virtual_input_name}
    if not entry.get("target_device"):
        return None
    built = {"target_device": entry["target_device"]}
    target_system = (entry.get("target_system") or "tuya").strip()
    if target_system == "satel_output":
        built["target_system"] = "satel_output"
    elif entry.get("dimmer_level_percent") not in (None, ""):
        built["dimmer_level_percent"] = int(entry["dimmer_level_percent"])
    return built


def _build_motion_rules(rules_form: list[dict]) -> list[dict]:
    rules = []
    for rule in rules_form:
        sources_form = rule.get("sources")
        if sources_form:
            built_sources = [s for s in (_build_motion_rule_source(e) for e in sources_form) if s]
            if len(built_sources) < 2:
                continue
            built = {"sources": built_sources}
            if (rule.get("source_combinator") or "or").strip() == "and":
                built["source_combinator"] = "and"
            source = built_sources[0]["source"]
        else:
            source = (rule.get("source") or "satel_zone").strip()
            single_source = _build_motion_rule_source(rule)
            if single_source is None:
                continue
            built = dict(single_source)

        name = (rule.get("name") or "").strip()
        if name:
            built["name"] = name

        scene_name = (rule.get("scene") or "").strip()
        virtual_input_name = (rule.get("virtual_input") or "").strip()
        actions_form = rule.get("actions")
        if scene_name:
            built["scene"] = scene_name
        elif actions_form:
            built_actions = [a for a in (_build_motion_rule_action(e) for e in actions_form) if a]
            if not built_actions:
                continue
            built["actions"] = built_actions
            if rule.get("action") == "turn_off":
                built["action"] = "turn_off"
            elif rule.get("duration_seconds") not in (None, ""):
                built["duration_seconds"] = int(rule["duration_seconds"])
        elif virtual_input_name:
            built["virtual_input"] = virtual_input_name
            if rule.get("duration_seconds") not in (None, ""):
                built["duration_seconds"] = int(rule["duration_seconds"])
        else:
            built["target_device"] = rule["target_device"]
            target_system = (rule.get("target_system") or "tuya").strip()
            if target_system == "satel_output":
                built["target_system"] = "satel_output"
            if rule.get("action") == "turn_off":
                built["action"] = "turn_off"
            else:
                if rule.get("duration_seconds") not in (None, ""):
                    built["duration_seconds"] = int(rule["duration_seconds"])
                if target_system != "satel_output":
                    built["dimmer_level_percent"] = (
                        int(rule["dimmer_level_percent"])
                        if rule.get("dimmer_level_percent") not in (None, "") else None
                    )

        if source == "satel_zone":
            trigger_on = (rule.get("trigger_on") or "violated").strip()
            if trigger_on == "restored":
                built["trigger_on"] = "restored"

        if rule.get("enabled") is False:
            built["enabled"] = False
        if rule.get("imported_to_flows"):
            built["imported_to_flows"] = True

        window = rule.get("time_window") or {}
        if window.get("enabled") and window.get("start") and window.get("end"):
            built["time_window"] = {"start": window["start"].strip(), "end": window["end"].strip()}

        escalation = rule.get("escalation") or {}
        if escalation.get("enabled") and escalation.get("escalated_scene"):
            built["escalation"] = {
                "threshold_count": int(escalation["threshold_count"]),
                "window_seconds": int(escalation["window_seconds"]),
                "escalated_scene": escalation["escalated_scene"].strip(),
            }

        condition = _build_condition(rule.get("condition"))
        if condition:
            built["condition"] = condition

        chain_target = (rule.get("then_trigger_rule") or "").strip()
        if chain_target:
            built["then_trigger_rule"] = chain_target

        rules.append(built)
    return rules


def _build_presence_simulation(form: dict) -> dict:
    ps = form.get("presence_simulation") or {}
    log_level = ps.get("log_level")

    if not ps.get("enabled"):
        built = {"enabled": False}
        if log_level:
            built["log_level"] = log_level
        return built

    built = {
        "enabled": True,
        "devices": [d for d in ps.get("devices", []) if d],
        "min_interval_minutes": int(ps.get("min_interval_minutes") or 10),
        "max_interval_minutes": int(ps.get("max_interval_minutes") or 40),
    }
    window = ps.get("window") or {}
    if window.get("enabled") and window.get("start") and window.get("end"):
        built["window"] = {"start": window["start"].strip(), "end": window["end"].strip()}
    if log_level:
        built["log_level"] = log_level
    return built


def _build_partitions(form: dict) -> dict:
    partitions = {p["name"]: int(p["number"]) for p in form.get("partitions", []) if p.get("name") and p.get("number")}
    return partitions or {"Dom": 1}


def _build_monitored_zones(form: dict) -> dict:
    return {z["name"]: int(z["number"]) for z in form.get("monitored_zones", []) if z.get("name") and z.get("number")}


def _build_monitored_outputs(form: dict) -> dict:
    return {o["name"]: int(o["number"]) for o in form.get("monitored_outputs", []) if o.get("name") and o.get("number")}


SATEL_ZONE_TYPES = {
    "drzwi": "Drzwi", "okno": "Okno", "ruch": "Czujka ruchu (PIR)",
    "dym": "Czujka dymu", "zalanie": "Czujka zalania", "wibracja": "Czujka wibracji/stłuczenia",
    "sabotaz": "Sabotaż/24h", "inne": "Inne",
}
SATEL_OUTPUT_TYPES = {
    "syrena": "Syrena", "przekaznik": "Przekaźnik/gniazdko", "oswietlenie": "Oświetlenie",
    "brama": "Brama/rolety", "ogrzewanie": "Ogrzewanie", "inne": "Inne",
}


def _build_zone_types(form: dict) -> dict:
    return {
        z["name"]: z["type"] for z in form.get("monitored_zones", [])
        if z.get("name") and z.get("number") and z.get("type") in SATEL_ZONE_TYPES
    }


def _build_output_types(form: dict) -> dict:
    return {
        o["name"]: o["type"] for o in form.get("monitored_outputs", [])
        if o.get("name") and o.get("number") and o.get("type") in SATEL_OUTPUT_TYPES
    }


def _build_virtual_inputs(form: dict) -> dict:
    return {
        vi["name"]: int(vi["number"])
        for vi in form.get("virtual_inputs", [])
        if vi.get("name") and vi.get("number")
    }


def _build_tuya_devices(form: dict) -> dict:
    devices = {}
    for d in form.get("tuya_devices", []):
        if not d.get("name"):
            continue
        gateway_name = (d.get("gateway") or "").strip()
        if gateway_name:
            entry = {"id": d["id"], "gateway": gateway_name, "type": d.get("type", "socket")}
            if d.get("cid"):
                entry["cid"] = d["cid"]
        else:
            entry = {
                "id": d["id"], "ip": d["ip"], "local_key": d["local_key"],
                "version": float(d.get("version", 3.4)), "type": d.get("type", "socket"),
            }
        if d.get("type") == "dimmer":
            entry["dimmer_dp"] = d["dimmer_dp"]
            entry["dimmer_range"] = [10, 1000]
        if d.get("dp_schema"):
            entry["dp_schema"] = d["dp_schema"]
        devices[d["name"]] = entry
    return devices


def _build_zigbee_gateways(form: dict) -> dict:
    return {
        g["name"]: {
            "id": g["id"], "ip": g["ip"], "local_key": g["local_key"],
            "version": float(g.get("version", 3.4)),
        }
        for g in form.get("zigbee_gateways", [])
        if g.get("name") and g.get("id") and g.get("ip") and g.get("local_key")
    }


def _build_cloud_devices(form: dict) -> dict:
    return {
        d["name"]: {
            "id": d["id"], "category": d.get("category", ""),
            "switch_code": (d.get("switch_code") or "switch_1").strip(),
            "dp_schema": d.get("dp_schema") or {},
        }
        for d in form.get("cloud_devices", [])
        if d.get("name") and d.get("id")
    }


def validate_connections(form: dict) -> list[str]:
    errors = []
    satel = form.get("satel", {})
    if not satel.get("host"):
        errors.append("Satel: adres IP centrali/modułu ETHM jest wymagany")

    ebus = form.get("ebus", {})
    if not ebus.get("mqtt_host"):
        errors.append("eBUS: adres brokera MQTT jest wymagany")

    tuya_cloud = form.get("tuya_cloud", {})
    if tuya_cloud.get("enabled") and not tuya_cloud.get("access_id"):
        errors.append("Tuya Cloud: Access ID jest wymagany, jeśli integracja włączona")

    ha = form.get("home_assistant", {})
    if ha.get("enabled") and not (ha.get("mqtt_host") or ebus.get("mqtt_host")):
        errors.append("Home Assistant: adres brokera MQTT jest wymagany, jeśli integracja włączona")

    satel_interval = satel.get("status_poll_interval_seconds")
    if satel_interval not in (None, ""):
        try:
            if int(satel_interval) < 2:
                errors.append("Satel: interwał odpytywania musi wynosić co najmniej 2 sekundy")
        except (TypeError, ValueError):
            errors.append("Satel: interwał odpytywania musi być liczbą")

    tuya_local_interval = form.get("tuya_local", {}).get("status_poll_interval_seconds")
    if tuya_local_interval not in (None, ""):
        try:
            if int(tuya_local_interval) < 3:
                errors.append("Tuya (lokalnie): interwał odpytywania musi wynosić co najmniej 3 sekundy")
        except (TypeError, ValueError):
            errors.append("Tuya (lokalnie): interwał odpytywania musi być liczbą")

    return errors


def build_connections_patch(form: dict, existing: dict) -> dict:
    config = copy.deepcopy(existing)
    satel_form = form.get("satel", {})
    ebus_form = form.get("ebus", {})

    config.setdefault("satel", {})
    config["satel"]["host"] = satel_form.get("host", "").strip()
    config["satel"]["port"] = int(satel_form.get("port") or config["satel"].get("port") or 7094)
    config["satel"]["partition"] = int(satel_form.get("partition") or config["satel"].get("partition") or 1)
    if (satel_form.get("integration_key") or "").strip():
        config["satel"]["integration_key"] = satel_form["integration_key"].strip()
    else:
        config["satel"].setdefault("integration_key", "")
    if (satel_form.get("encryption_key") or "").strip():
        config["satel"]["encryption_key"] = satel_form["encryption_key"].strip()
    elif satel_form.get("encryption_key_clear"):
        config["satel"]["encryption_key"] = None
    else:
        config["satel"].setdefault("encryption_key", None)
    config["satel"]["encryption_enabled"] = bool(satel_form.get("encryption_enabled"))
    config["satel"].setdefault("virtual_inputs", {})
    vi_mode = satel_form.get("virtual_inputs_mode") or config["satel"]["virtual_inputs"].get("mode") or "library"
    config["satel"]["virtual_inputs"]["mode"] = vi_mode
    config["satel"]["virtual_inputs"]["port"] = int(
        satel_form.get("virtual_inputs_port") or config["satel"]["virtual_inputs"].get("port") or 8888
    )
    config["satel"]["virtual_inputs"].setdefault("inputs", {})
    config["satel"].setdefault("outputs", {})
    config["satel"].setdefault("fields_file", "satel_fields.yaml")
    config["satel"]["status_poll_interval_seconds"] = int(
        satel_form.get("status_poll_interval_seconds")
        or config["satel"].get("status_poll_interval_seconds") or 3
    )

    config.setdefault("ebus", {})
    config["ebus"]["mqtt_host"] = ebus_form.get("mqtt_host", "").strip()
    config["ebus"]["mqtt_port"] = int(ebus_form.get("mqtt_port") or config["ebus"].get("mqtt_port") or 1883)
    config["ebus"].setdefault("sensors_file", "sensors.yaml")

    config.setdefault("boiler_output", {})
    config["boiler_output"].setdefault("mqtt", {})
    config["boiler_output"]["mqtt"]["host"] = config["ebus"]["mqtt_host"]
    config["boiler_output"]["mqtt"]["port"] = config["ebus"]["mqtt_port"]
    config["boiler_output"]["mqtt"]["topic_prefix"] = (
        (ebus_form.get("topic_prefix") or "").strip()
        or config["boiler_output"]["mqtt"].get("topic_prefix", "home/boiler")
    )
    config["boiler_output"].setdefault("tuya_virtual_device", {"device_id": ""})

    existing_mqtt_publish = config.get("mqtt_publish", {})
    mqtt_publish_form = form.get("mqtt_publish", {})
    config["mqtt_publish"] = {
        "enabled": bool(mqtt_publish_form.get("enabled", True)),
        "topics": {
            "satel": (mqtt_publish_form.get("satel_topic") or "satel").strip(),
            "tuya": (mqtt_publish_form.get("tuya_topic") or "tuya").strip(),
            "homewhiz": (mqtt_publish_form.get("homewhiz_topic") or "homewhiz").strip(),
        },
    }
    if existing_mqtt_publish.get("log_level"):
        config["mqtt_publish"]["log_level"] = existing_mqtt_publish["log_level"]

    config.setdefault("tuya", {})
    tuya_cloud_form = form.get("tuya_cloud", {})
    if tuya_cloud_form.get("enabled") and (tuya_cloud_form.get("access_id") or "").strip():
        existing_cloud = config["tuya"].get("cloud", {})
        config["tuya"]["cloud"] = {
            "enabled": existing_cloud.get("enabled", True),
            "access_id": tuya_cloud_form["access_id"].strip(),
            "access_key": (tuya_cloud_form.get("access_key") or "").strip() or existing_cloud.get("access_key", ""),
            "api_region": (tuya_cloud_form.get("api_region") or "").strip() or existing_cloud.get("api_region", "eu"),
            "api_endpoint": (tuya_cloud_form.get("api_endpoint") or "").strip()
                or existing_cloud.get("api_endpoint", "https://openapi.tuyaeu.com"),
            "mq_endpoint": (tuya_cloud_form.get("mq_endpoint") or "").strip()
                or existing_cloud.get("mq_endpoint", "wss://mqe.tuyaeu.com:8285/"),
            "camera_device_ids": existing_cloud.get("camera_device_ids", []),
        }
        if existing_cloud.get("log_level"):
            config["tuya"]["cloud"]["log_level"] = existing_cloud["log_level"]
    else:
        config["tuya"].pop("cloud", None)
    tuya_local_form = form.get("tuya_local", {})
    config["tuya"]["status_poll_interval_seconds"] = int(
        tuya_local_form.get("status_poll_interval_seconds")
        or config["tuya"].get("status_poll_interval_seconds") or 30
    )
    config["tuya"].setdefault("devices_file", "tuya_devices.yaml")

    ha_form = form.get("home_assistant", {})
    existing_ha = config.get("home_assistant", {})
    if ha_form.get("enabled"):
        config["home_assistant"] = {
            "enabled": True,
            "mqtt_host": (ha_form.get("mqtt_host") or "").strip() or config["ebus"]["mqtt_host"],
            "mqtt_port": int(ha_form.get("mqtt_port") or existing_ha.get("mqtt_port") or 1883),
            "discovery_prefix": existing_ha.get("discovery_prefix", "homeassistant"),
            "state_topic_prefix": existing_ha.get("state_topic_prefix", "home/bridge"),
            "device_name": existing_ha.get("device_name", "Satel-Tuya-eBUS Bridge"),
        }
        if existing_ha.get("log_level"):
            config["home_assistant"]["log_level"] = existing_ha["log_level"]
    else:
        config["home_assistant"] = {**existing_ha, "enabled": False}

    notif_form = form.get("notifications", {})
    config.setdefault("notifications", {})
    config["notifications"]["ntfy_topic_url"] = (
        notif_form.get("ntfy_topic_url", "") or ""
    ).strip() or config["notifications"].get("ntfy_topic_url", "")
    config["notifications"].setdefault("grace_seconds", 120)

    return config


def connections_to_form(existing: dict) -> dict:
    satel = existing.get("satel", {})
    tuya = existing.get("tuya", {})
    ebus = existing.get("ebus", {})
    tuya_cloud = tuya.get("cloud", {})
    ha = existing.get("home_assistant", {})
    mqtt_publish = existing.get("mqtt_publish", {})
    boiler_mqtt = existing.get("boiler_output", {}).get("mqtt", {})
    return {
        "satel": {
            "host": satel.get("host", ""),
            "port": satel.get("port", 7094),
            "partition": satel.get("partition", 1),
            "virtual_inputs_mode": satel.get("virtual_inputs", {}).get("mode", "library"),
            "virtual_inputs_port": satel.get("virtual_inputs", {}).get("port", 8888),
            "integration_key_set": bool(satel.get("integration_key")),
            "encryption_key_set": bool(satel.get("encryption_key")),
            "encryption_enabled": bool(satel.get("encryption_enabled")),
            "status_poll_interval_seconds": satel.get("status_poll_interval_seconds", 3),
        },
        "tuya_local": {
            "status_poll_interval_seconds": tuya.get("status_poll_interval_seconds", 30),
        },
        "ebus": {
            "mqtt_host": ebus.get("mqtt_host", ""),
            "mqtt_port": ebus.get("mqtt_port", 1883),
            "topic_prefix": boiler_mqtt.get("topic_prefix", "home/boiler"),
        },
        "mqtt_publish": {
            "enabled": mqtt_publish.get("enabled", True),
            "satel_topic": mqtt_publish.get("topics", {}).get("satel", "satel"),
            "tuya_topic": mqtt_publish.get("topics", {}).get("tuya", "tuya"),
            "homewhiz_topic": mqtt_publish.get("topics", {}).get("homewhiz", "homewhiz"),
        },
        "tuya_cloud": {
            "enabled": bool(tuya_cloud),
            "access_id": tuya_cloud.get("access_id", ""),
            "access_key_set": bool(tuya_cloud.get("access_key")),
            "api_region": tuya_cloud.get("api_region", "eu"),
            "api_endpoint": tuya_cloud.get("api_endpoint", "https://openapi.tuyaeu.com"),
            "mq_endpoint": tuya_cloud.get("mq_endpoint", "wss://mqe.tuyaeu.com:8285/"),
        },
        "home_assistant": {
            "enabled": bool(ha.get("enabled")),
            "mqtt_host": ha.get("mqtt_host", ""),
            "mqtt_port": ha.get("mqtt_port", 1883),
        },
        "notifications": {
            "ntfy_topic_url": existing.get("notifications", {}).get("ntfy_topic_url", ""),
        },
    }


def validate_satel_connection(form: dict) -> list[str]:
    errors = []
    if not (form.get("host") or "").strip():
        errors.append("Adres IP centrali/modułu ETHM jest wymagany")
    return errors


def satel_connection_to_form(satel_section: dict) -> dict:
    return {
        "host": satel_section.get("host", ""),
        "port": satel_section.get("port", 7094),
        "partition": satel_section.get("partition", 1),
        "integration_key_set": bool(satel_section.get("integration_key")),
        "encryption_key_set": bool(satel_section.get("encryption_key")),
        "encryption_enabled": bool(satel_section.get("encryption_enabled")),
        "virtual_inputs_mode": satel_section.get("virtual_inputs", {}).get("mode", "library"),
        "virtual_inputs_port": satel_section.get("virtual_inputs", {}).get("port", 8888),
    }


def build_satel_connection_patch(form: dict, existing_satel_section: dict) -> dict:
    section = copy.deepcopy(existing_satel_section)
    section["host"] = (form.get("host") or "").strip()
    section["port"] = int(form.get("port") or section.get("port") or 7094)
    section["partition"] = int(form.get("partition") or section.get("partition") or 1)
    if (form.get("integration_key") or "").strip():
        section["integration_key"] = form["integration_key"].strip()
    else:
        section.setdefault("integration_key", "")
    if (form.get("encryption_key") or "").strip():
        section["encryption_key"] = form["encryption_key"].strip()
    elif form.get("encryption_key_clear"):
        section["encryption_key"] = None
    else:
        section.setdefault("encryption_key", None)
    section["encryption_enabled"] = bool(form.get("encryption_enabled"))
    section.setdefault("virtual_inputs", {})
    vi_mode = form.get("virtual_inputs_mode") or section["virtual_inputs"].get("mode") or "library"
    section["virtual_inputs"]["mode"] = vi_mode
    section["virtual_inputs"]["port"] = int(
        form.get("virtual_inputs_port") or section["virtual_inputs"].get("port") or 8888
    )
    section["virtual_inputs"].setdefault("inputs", {})
    section.setdefault("outputs", {})
    section.setdefault("fields_file", "satel_fields.yaml")
    section.setdefault("status_poll_interval_seconds", 3)
    return section


def validate_ebus_connection(form: dict) -> list[str]:
    errors = []
    if not (form.get("mqtt_host") or "").strip():
        errors.append("Adres brokera MQTT jest wymagany")
    return errors


def ebus_connection_to_form(ebus_section: dict, boiler_output_section: dict) -> dict:
    return {
        "mqtt_host": ebus_section.get("mqtt_host", ""),
        "mqtt_port": ebus_section.get("mqtt_port", 1883),
        "topic_prefix": boiler_output_section.get("mqtt", {}).get("topic_prefix", "home/boiler"),
    }


def build_ebus_connection_patch(form: dict, existing_config: dict) -> dict:
    config = copy.deepcopy(existing_config)
    config.setdefault("ebus", {})
    config["ebus"]["mqtt_host"] = (form.get("mqtt_host") or "").strip()
    config["ebus"]["mqtt_port"] = int(form.get("mqtt_port") or config["ebus"].get("mqtt_port") or 1883)
    config["ebus"].setdefault("sensors_file", "sensors.yaml")

    config.setdefault("boiler_output", {})
    config["boiler_output"].setdefault("mqtt", {})
    config["boiler_output"]["mqtt"]["host"] = config["ebus"]["mqtt_host"]
    config["boiler_output"]["mqtt"]["port"] = config["ebus"]["mqtt_port"]
    config["boiler_output"]["mqtt"]["topic_prefix"] = (
        (form.get("topic_prefix") or "").strip()
        or config["boiler_output"]["mqtt"].get("topic_prefix", "home/boiler")
    )
    config["boiler_output"].setdefault("tuya_virtual_device", {"device_id": ""})
    return config


def validate_location(form: dict) -> list[str]:
    errors = []
    lat, lon = form.get("latitude"), form.get("longitude")
    if lat in (None, "") and lon in (None, ""):
        return errors
    if lat in (None, "") or lon in (None, ""):
        errors.append("Lokalizacja: podaj oba pola (szerokość i długość geograficzna) albo żadne")
        return errors
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        errors.append("Lokalizacja: szerokość i długość geograficzna muszą być liczbami")
        return errors
    if not (-90 <= lat_f <= 90):
        errors.append("Lokalizacja: szerokość geograficzna musi być w zakresie -90..90")
    if not (-180 <= lon_f <= 180):
        errors.append("Lokalizacja: długość geograficzna musi być w zakresie -180..180")
    return errors


def build_location_patch(form: dict, existing: dict) -> dict:
    config = copy.deepcopy(existing)
    lat, lon = form.get("latitude"), form.get("longitude")
    if lat in (None, "") or lon in (None, ""):
        config.pop("location", None)
    else:
        config["location"] = {"latitude": float(lat), "longitude": float(lon)}
    return config


def list_plugins(config_raw: dict) -> list[dict]:
    from bridge.logging_config import plugin_config_section

    metas, _errors = plugins.discover_plugin_metas()

    result = []
    for meta in metas:
        state = meta.describe_state(config_raw) if meta.describe_state else {"enabled": False, "configured": False, "detail": ""}
        result.append({
            "id": meta.id,
            "name": meta.name,
            "icon": meta.icon,
            "description": meta.description,
            "category": meta.category,
            "config_url": meta.config_url,
            "toggleable": meta.toggleable,
            "restart_required": meta.restart_required,
            "has_log_level": meta.has_log_level,
            "has_poll_interval": meta.has_poll_interval,
            "min_poll_interval_seconds": meta.min_poll_interval_seconds,
            "log_level": plugin_config_section(config_raw, meta.id).get("log_level") or "",
            **state,
        })
    return result


def list_plugin_load_errors() -> list[dict]:
    _metas, errors = plugins.discover_plugin_metas()
    return [{"plugin_dir": e.plugin_dir, "error": e.error} for e in errors]


def dict_to_name_number_rows(d: dict) -> list[dict]:
    return [{"name": name, "number": number} for name, number in d.items()]


def zones_to_rows(numbers: dict, types: dict) -> list[dict]:
    return [{"name": name, "number": number, "type": types.get(name, "")} for name, number in numbers.items()]


def virtual_inputs_to_rows(inputs: dict) -> list[dict]:
    rows = []
    for name, value in inputs.items():
        number = value.get("number") if isinstance(value, dict) else value
        rows.append({"name": name, "number": number})
    return rows


def tuya_devices_to_rows(devices: dict) -> list[dict]:
    return [
        {
            "name": name, "id": cfg.get("id", ""), "ip": cfg.get("ip", ""),
            "local_key": cfg.get("local_key", ""), "type": cfg.get("type", "socket"),
            "dimmer_dp": cfg.get("dimmer_dp", ""),
            "gateway": cfg.get("gateway", ""), "cid": cfg.get("cid", ""),
            "dp_schema": cfg.get("dp_schema") or {},
        }
        for name, cfg in devices.items()
    ]


def zigbee_gateways_to_rows(gateways: dict) -> list[dict]:
    return [
        {
            "name": name, "id": cfg.get("id", ""), "ip": cfg.get("ip", ""),
            "local_key": cfg.get("local_key", ""), "version": cfg.get("version", 3.4),
        }
        for name, cfg in gateways.items()
    ]


def cloud_devices_to_rows(devices: dict) -> list[dict]:
    return [
        {
            "name": name, "id": cfg.get("id", ""), "category": cfg.get("category", ""),
            "switch_code": cfg.get("switch_code", "switch_1"), "dp_schema": cfg.get("dp_schema", {}),
        }
        for name, cfg in devices.items()
    ]


def build_satel_fields_dict(form: dict) -> dict:
    return {
        "monitored_zones": _build_monitored_zones(form),
        "monitored_outputs": _build_monitored_outputs(form),
        "partitions": _build_partitions(form),
        "virtual_inputs": _build_virtual_inputs(form),
        "zone_types": _build_zone_types(form),
        "output_types": _build_output_types(form),
    }


def build_tuya_devices_dict(form: dict) -> dict:
    return {
        "devices": _build_tuya_devices(form),
        "zigbee_gateways": _build_zigbee_gateways(form),
        "cloud_devices": _build_cloud_devices(form),
    }


def clean_sensors_payload(payload) -> tuple[list | None, str | None]:
    if not isinstance(payload, list):
        return None, "Oczekiwano listy czujników w ciele żądania"

    cleaned = []
    seen_names = set()
    for i, item in enumerate(payload):
        name = (item.get("name") or "").strip()
        mqtt_topic = (item.get("mqtt_topic") or "").strip()
        if not name or not mqtt_topic:
            return None, f"Wiersz {i + 1}: pola 'name' i 'mqtt_topic' są wymagane"
        if name in seen_names:
            return None, f"Zduplikowana nazwa czujnika: '{name}'"
        seen_names.add(name)

        targets = item.get("targets") or {}
        cleaned.append({
            "name": name,
            "label": (item.get("label") or name).strip(),
            "mqtt_topic": mqtt_topic,
            "mqtt_field": (item.get("mqtt_field") or "").strip(),
            "targets": {
                "history": bool(targets.get("history", True)),
                "mqtt_publish": bool(targets.get("mqtt_publish", False)),
                "tuya_virtual_device": bool(targets.get("tuya_virtual_device", False)),
                "tuya_dp": (targets.get("tuya_dp") or "").strip(),
            },
        })
    return cleaned, None


def build_automations_dict(form: dict) -> dict:
    return {
        "scenes": _build_scenes(form.get("scenes", [])),
        "reguły_ruchu": _build_motion_rules(form.get("motion_rules", [])),
        "presence_simulation": _build_presence_simulation(form),
        "flows": build_flows(form.get("flows", [])),
    }


def _build_flow_node_config(ntype: str, c: dict) -> dict:
    if ntype == "trigger":
        source = c.get("source") or "satel_zone"
        cfg = {"source": source, "op": c.get("op") or ("violated" if source == "satel_zone" else "changes")}
        if source == "satel_zone":
            cfg["zone_number"] = int(c["zone_number"])
        else:
            cfg["source_device"] = c.get("source_device")
            if c.get("trigger_value") not in (None, ""):
                cfg["trigger_value"] = c["trigger_value"]
        return cfg
    if ntype == "condition":
        return {"op": c.get("op"), "value": c.get("value")}
    if ntype == "device":
        cfg = {
            "target_device": c.get("target_device"), "target_system": c.get("target_system") or "tuya",
            "value": c.get("value") or "on",
        }
        if c.get("dimmer_level_percent") not in (None, ""):
            cfg["dimmer_level_percent"] = int(c["dimmer_level_percent"])
        return cfg
    if ntype == "scene":
        return {"scene_name": c.get("scene_name")}
    if ntype == "delay":
        return {"seconds": float(c.get("seconds") or 0)}
    if ntype == "notify":
        return {"level": c.get("level") or "info", "message": c.get("message") or ""}
    if ntype == "mqttIn":
        return {"topic": c.get("topic")}
    if ntype == "mqttOut":
        cfg = {"topic": c.get("topic")}
        if c.get("payload") not in (None, ""):
            cfg["payload"] = c["payload"]
        return cfg
    if ntype == "http":
        cfg = {"method": (c.get("method") or "GET").upper(), "url": c.get("url")}
        if c.get("body") not in (None, ""):
            cfg["body"] = c["body"]
        return cfg
    if ntype == "schedule":
        return {"start": c.get("start"), "end": c.get("end")}
    if ntype == "escalation":
        return {"threshold_count": int(c["threshold_count"]), "window_seconds": float(c["window_seconds"])}
    if ntype == "state":
        return {"key": c.get("key"), "op": c.get("op") or "equals", "value": c.get("value")}
    if ntype == "condition_group":
        return {"condition": _build_condition(c.get("condition"))}
    if ntype == "tuya_dp":
        return {"target_device": c.get("target_device"), "dp_code": c.get("dp_code"), "value": c.get("value")}
    if ntype == "stop":
        return {}
    return dict(c)


def _build_flow_node(node: dict) -> dict:
    built = {
        "id": node["id"], "type": node["type"],
        "config": _build_flow_node_config(node["type"], node.get("config") or {}),
        "wires": [[str(t) for t in (port or [])] for port in (node.get("wires") or [])],
    }
    if node.get("x") is not None:
        built["x"] = node["x"]
    if node.get("y") is not None:
        built["y"] = node["y"]
    return built


def build_flows(flows_form: list[dict]) -> list[dict]:
    flows = []
    for flow in flows_form:
        name = (flow.get("name") or "").strip()
        if not name:
            continue
        built_nodes = [_build_flow_node(n) for n in (flow.get("nodes") or []) if n.get("id") and n.get("type")]
        if not built_nodes:
            continue
        built = {"id": flow.get("id") or name, "name": name, "nodes": built_nodes}
        if flow.get("enabled") is False:
            built["enabled"] = False
        flows.append(built)
    return flows


def motion_rule_to_flow(rule: dict, index: int) -> tuple[dict | None, list[str]]:
    """Konwertuje POJEDYNCZĄ regułę ruchu (`reguły_ruchu`) na flow (graf węzłów),
    dla przycisku "Importuj z reguł ruchu" w edytorze Flow. Zwraca
    (flow_albo_None, ostrzeżenia) - None, gdy reguła używa mechanizmu, którego
    Flow jeszcze nie ma (wiele źródeł/akcji) - wtedy CAŁA reguła jest pomijana
    z ostrzeżeniem zamiast tworzyć okrojony flow. Zwrócony flow NIE jest
    jeszcze zapisany - wywołujący (endpoint /api/flows/import-from-motion-
    rules) oddaje go do przeglądu w panelu przed zapisem."""
    warnings: list[str] = []
    name = (rule.get("name") or "").strip() or f"Reguła ruchu #{index + 1}"

    if rule.get("imported_to_flows"):
        return None, [f"'{name}': już zaimportowano wcześniej do Flow - pomięto"]

    if rule.get("sources") or rule.get("actions"):
        return None, [f"'{name}': wiele źródeł/akcji (sources/actions) nie jest jeszcze obsługiwane w konwerterze - pomięto całą regułę"]

    if rule.get("condition"):
        warnings.append(f"'{name}': dodatkowy warunek (condition) NIE został przeniesiony - dodaj go ręcznie jeśli jest potrzebny")
    if rule.get("then_trigger_rule"):
        warnings.append(
            f"'{name}': wyzwalanie łańcuchowe (then_trigger_rule='{rule['then_trigger_rule']}') NIE zostało przeniesione"
        )

    source = rule.get("source") or "satel_zone"
    source_cfg = {"source": source}
    if source == "satel_zone":
        source_cfg["zone_number"] = rule.get("zone_number")
    else:
        source_cfg["source_device"] = rule.get("source_device")
        source_cfg["trigger_value"] = rule.get("trigger_value")

    has_scene = "scene" in rule
    until_restored = (
        not has_scene and rule.get("action") != "turn_off" and rule.get("duration_seconds") in (None, 0)
    )
    escalation_cfg = rule.get("escalation")
    if escalation_cfg and until_restored:
        warnings.append(
            f"'{name}': eskalacja pominięta dla trybu \"do ustania naruszenia\" (rzadka kombinacja) - dodaj ręcznie jeśli potrzebna"
        )

    node_counter = [0]
    nodes: list[dict] = []

    def next_id() -> str:
        node_counter[0] += 1
        return f"n{node_counter[0]}"

    def link(src: dict, port: int, dst: dict) -> None:
        src["wires"][port].append(dst["id"])

    def build_pipeline(op: str, on_value: str | None, row_y: int, after_action_duration: float | None = None) -> None:
        col = [0]

        def add_node(ntype: str, config: dict, outputs: int = 1) -> dict:
            node = {
                "id": next_id(), "type": ntype, "config": config,
                "wires": [[] for _ in range(outputs)], "x": 40 + 220 * col[0], "y": row_y,
            }
            col[0] += 1
            nodes.append(node)
            return node

        current = add_node("trigger", {**source_cfg, "op": op})

        window = rule.get("time_window")
        if window:
            sched = add_node("schedule", {"start": window.get("start"), "end": window.get("end")})
            link(current, 0, sched)
            current = sched

        action_port = 0
        if escalation_cfg and not until_restored:
            esc = add_node("escalation", {
                "threshold_count": escalation_cfg.get("threshold_count"),
                "window_seconds": escalation_cfg.get("window_seconds"),
            }, outputs=2)
            link(current, 0, esc)
            escalated = add_node("scene", {"scene_name": escalation_cfg.get("escalated_scene")})
            link(esc, 1, escalated)
            current = esc

        if has_scene:
            action = add_node("scene", {"scene_name": rule["scene"]})
        elif "virtual_input" in rule:
            action = add_node("device", {
                "target_device": rule["virtual_input"], "target_system": "satel_virtual_input", "value": on_value,
            })
        else:
            action_cfg = {
                "target_device": rule.get("target_device"), "target_system": rule.get("target_system") or "tuya",
                "value": on_value,
            }
            if on_value != "off" and rule.get("dimmer_level_percent") is not None:
                action_cfg["dimmer_level_percent"] = rule.get("dimmer_level_percent")
            action = add_node("device", action_cfg)
        link(current, action_port, action)

        if after_action_duration:
            delay = add_node("delay", {"seconds": after_action_duration})
            link(action, 0, delay)
            if "virtual_input" in rule:
                off_action = add_node("device", {
                    "target_device": rule["virtual_input"], "target_system": "satel_virtual_input", "value": "off",
                })
            else:
                off_action = add_node("device", {
                    "target_device": rule.get("target_device"), "target_system": rule.get("target_system") or "tuya",
                    "value": "off",
                })
            link(delay, 0, off_action)

    if has_scene:
        build_pipeline(rule.get("trigger_on", "violated"), None, 60)
    elif until_restored:
        build_pipeline("violated", "on", 60)
        build_pipeline("restored", "off", 240)
    elif rule.get("action") == "turn_off":
        build_pipeline(rule.get("trigger_on", "violated"), "off", 60)
    else:
        build_pipeline(
            rule.get("trigger_on", "violated"), "on", 60,
            after_action_duration=rule.get("duration_seconds"),
        )

    flow = {"id": f"flow_import_{index}_{uuid.uuid4().hex[:6]}", "name": f"Import: {name}", "enabled": True, "nodes": nodes}
    return flow, warnings


def flow_to_motion_rule(flow: dict, index: int) -> tuple[dict | None, list[str]]:
    """Konwertuje POJEDYNCZE flow na regułę ruchu (`reguły_ruchu`) - odwrotność
    motion_rule_to_flow, dla przycisku "Eksportuj zaznaczone do Automatyzacje" w
    edytorze Flow. Obsługuje TYLKO liniowe grafy o kształcie, jaki sama tworzy
    motion_rule_to_flow: jeden węzeł Trigger (bez MQTT in), opcjonalnie
    Harmonogram, opcjonalnie Eskalacja (z gałęzią "eskalacja" do dokładnie
    jednej Sceny), opcjonalnie JEDEN węzeł Warunek stanu albo Warunek złożony,
    a na końcu Scena ALBO Urządzenie - opcjonalnie Urządzenie(włącz) ->
    Opóźnienie -> Urządzenie(wyłącz, TO SAMO urządzenie) jako odpowiednik
    duration_seconds. Rozgałęzienia (kilka połączeń z jednego wyjścia), węzły
    MQTT/HTTP/Powiadomienie, zwykły węzeł Warunek (porównanie payloadu) i
    akcja "Zmiana stanu" NIE mają odpowiednika w regule ruchu - flow jest
    w całości pomijane z ostrzeżeniem opisującym dlaczego (zwraca None)."""
    name = (flow.get("name") or "").strip() or f"Flow #{index + 1}"
    if name.startswith("Import: "):
        name = name[len("Import: "):]
    nodes_by_id = {n["id"]: n for n in flow.get("nodes", [])}
    warnings: list[str] = []

    trigger_nodes = [n for n in flow.get("nodes", []) if n.get("type") == "trigger"]
    if len(trigger_nodes) != 1:
        return None, [f"'{name}': eksport do reguły ruchu wymaga dokładnie jednego węzła Trigger (bez MQTT in) - pomięto"]
    trigger = trigger_nodes[0]
    tcfg = trigger.get("config") or {}
    op = tcfg.get("op", "violated")
    if op == "changes":
        return None, [f"'{name}': wyzwalacz \"każda zmiana\" nie ma odpowiednika w regule ruchu - pomięto"]

    source = tcfg.get("source") or "satel_zone"
    built: dict = {"source": source}
    if source == "satel_zone":
        built["zone_number"] = tcfg.get("zone_number")
    else:
        built["source_device"] = tcfg.get("source_device")
        built["trigger_value"] = tcfg.get("trigger_value")
    if op == "restored":
        built["trigger_on"] = "restored"

    def next_node(node: dict, port: int = 0):
        wires = node.get("wires") or []
        targets = wires[port] if port < len(wires) else []
        if len(targets) > 1:
            return "branch"
        if not targets:
            return None
        return nodes_by_id.get(targets[0])

    current = trigger
    while True:
        nxt = next_node(current, 0)
        if nxt == "branch":
            return None, [f"'{name}': rozgałęzienie (kilka połączeń z jednego wyjścia) nie ma odpowiednika w regule ruchu - pomięto"]
        if nxt is None:
            return None, [f"'{name}': flow nie kończy się rozpoznawalną akcją (Scena albo Urządzenie) - pomięto"]
        ntype = nxt.get("type")
        ncfg = nxt.get("config") or {}

        if ntype == "schedule":
            if "time_window" in built:
                return None, [f"'{name}': wiele węzłów Harmonogram nieobsługiwane - pomięto"]
            built["time_window"] = {"start": ncfg.get("start"), "end": ncfg.get("end")}
            current = nxt
            continue

        if ntype == "escalation":
            if "escalation" in built:
                return None, [f"'{name}': wiele węzłów Eskalacja nieobsługiwane - pomięto"]
            esc_target = next_node(nxt, 1)
            if esc_target == "branch" or esc_target is None or esc_target.get("type") != "scene":
                return None, [f"'{name}': węzeł Eskalacja musi mieć dokładnie jedną Scenę na wyjściu \"eskalacja\" - pomięto"]
            built["escalation"] = {
                "threshold_count": ncfg.get("threshold_count"),
                "window_seconds": ncfg.get("window_seconds"),
                "escalated_scene": (esc_target.get("config") or {}).get("scene_name"),
            }
            current = nxt
            continue

        if ntype in ("state", "condition_group"):
            if "condition" in built:
                return None, [f"'{name}': wiele węzłów warunku nieobsługiwane - pomięto"]
            other = next_node(nxt, 1)
            if other == "branch" or other is not None:
                node_label = "Warunek stanu" if ntype == "state" else "Warunek złożony"
                warnings.append(f"'{name}': gałąź \"inaczej\" węzła {node_label} pominięta")
            if ntype == "state":
                leaf = {"type": "state", "key": ncfg.get("key")}
                if ncfg.get("op") == "not_equals":
                    leaf["not_equals"] = ncfg.get("value")
                else:
                    leaf["equals"] = ncfg.get("value")
                built["condition"] = leaf
            else:
                built["condition"] = ncfg.get("condition")
            current = nxt
            continue

        if ntype == "condition":
            return None, [f"'{name}': zwykły węzeł Warunek (porównanie payloadu) nie ma odpowiednika w regule ruchu - pomięto"]

        if ntype == "scene":
            if next_node(nxt, 0) is not None:
                warnings.append(f"'{name}': węzły po Scenie pominięte (reguła kończy się na scenie)")
            built["scene"] = ncfg.get("scene_name")
            break

        if ntype == "device":
            value = str(ncfg.get("value", "on")).lower()
            if value == "toggle":
                return None, [f"'{name}': akcja \"Zmiana stanu\" węzła Urządzenie nie ma odpowiednika w regule ruchu - pomięto"]
            target_device = ncfg.get("target_device")
            target_system = ncfg.get("target_system") or "tuya"
            if target_system == "satel_virtual_input":
                built["virtual_input"] = target_device
            else:
                built["target_device"] = target_device
                if target_system == "satel_output":
                    built["target_system"] = "satel_output"
                if value != "off" and ncfg.get("dimmer_level_percent") is not None:
                    built["dimmer_level_percent"] = ncfg.get("dimmer_level_percent")

            if value == "off":
                built["action"] = "turn_off"
                if next_node(nxt, 0) is not None:
                    warnings.append(f"'{name}': węzły po Urządzeniu (wyłącz) pominięte")
                break

            after = next_node(nxt, 0)
            if after == "branch":
                return None, [f"'{name}': rozgałęzienie po Urządzeniu (włącz) nieobsługiwane - pomięto"]
            if after is None:
                return None, [
                    f"'{name}': pojedynczy węzeł Urządzenie (włącz) bez opóźnienia/wyłączenia nie ma odpowiednika "
                    f"w regule ruchu (reguła zawsze wymaga czasu, sceny albo trybu do ustania naruszenia) - pomięto"
                ]
            if after.get("type") != "delay":
                return None, [f"'{name}': po Urządzeniu (włącz) oczekiwano węzła Opóźnienie - pomięto"]
            off_node = next_node(after, 0)
            if off_node == "branch" or off_node is None or off_node.get("type") != "device":
                return None, [f"'{name}': po Opóźnieniu oczekiwano węzła Urządzenie (wyłącz) - pomięto"]
            off_cfg = off_node.get("config") or {}
            if (
                str(off_cfg.get("value", "on")).lower() != "off"
                or off_cfg.get("target_device") != target_device
                or (off_cfg.get("target_system") or "tuya") != target_system
            ):
                return None, [f"'{name}': węzeł Urządzenie po Opóźnieniu musi wyłączać TO SAMO urządzenie - pomięto"]
            if next_node(off_node, 0) is not None:
                warnings.append(f"'{name}': węzły po Urządzeniu (wyłącz) pominięte")
            built["duration_seconds"] = (after.get("config") or {}).get("seconds")
            break

        return None, [f"'{name}': węzeł '{ntype}' nie ma odpowiednika w regule ruchu - pomięto całą regułę"]

    built["name"] = name
    return built, warnings


def build_config_dict(form: dict) -> dict:
    satel_form = form.get("satel", {})

    config = {
        "satel": {
            "host": satel_form["host"],
            "port": int(satel_form.get("port", 7094)),
            "integration_key": (satel_form.get("integration_key") or "").strip(),
            "encryption_key": (satel_form.get("encryption_key") or "").strip() or None,
            "encryption_enabled": bool(satel_form.get("encryption_enabled")),
            "partition": int(satel_form.get("partition", 1)),
            "virtual_inputs": {
                "mode": satel_form.get("virtual_inputs_mode") or "library",
                "port": int(satel_form.get("virtual_inputs_port") or 8888),
                "inputs": {},
            },
            "outputs": {},
            "fields_file": "satel_fields.yaml",
        },
        "tuya": {
            "status_poll_interval_seconds": 30,
            "devices_file": "tuya_devices.yaml",
        },
        "automations_file": "automations.yaml",
        "reguły_swiatlo_do_satela": [],
        "reguły_kamera_alarm": [],
        "alarm_rules": [],
        "boiler_rules": [],
        "ebus": {
            "mqtt_host": form["ebus"]["mqtt_host"],
            "mqtt_port": int(form["ebus"].get("mqtt_port", 1883)),
            "sensors_file": "sensors.yaml",
        },
        "boiler_output": {
            "mqtt": {"host": form["ebus"]["mqtt_host"], "port": int(form["ebus"].get("mqtt_port", 1883)),
                      "topic_prefix": "home/boiler"},
            "tuya_virtual_device": {"device_id": ""},
        },
        "dashboard": {
            "db_path": "bridge_history.db",
            "auth": {
                "username": (form.get("admin_username") or "admin").strip() or "admin",
                "password_hash": generate_password_hash(form["admin_password"]),
                "max_failed_attempts": 5,
                "lockout_seconds": 300,
            },
        },
        "maintenance": {
            "purge_enabled": True, "retention_days": 90,
            "backup_enabled": False, "backup_dir": "backups", "backup_keep_count": 7,
            "run_in_process": True, "interval_hours": 24,
        },
        "notifications": {
            "ntfy_topic_url": form.get("notifications", {}).get("ntfy_topic_url", ""),
            "grace_seconds": 120,
        },
    }

    if form.get("location", {}).get("latitude"):
        config["location"] = {
            "latitude": float(form["location"]["latitude"]),
            "longitude": float(form["location"]["longitude"]),
        }

    if form.get("home_assistant", {}).get("enabled"):
        config["home_assistant"] = {
            "enabled": True,
            "mqtt_host": form["home_assistant"].get("mqtt_host") or form["ebus"]["mqtt_host"],
            "mqtt_port": int(form["home_assistant"].get("mqtt_port", 1883)),
            "discovery_prefix": "homeassistant",
            "state_topic_prefix": "home/bridge",
            "device_name": "Satel-Tuya-eBUS Bridge",
        }

    if form.get("unifi_protect", {}).get("host"):
        config["unifi_protect"] = {
            "host": form["unifi_protect"]["host"],
            "username": form["unifi_protect"].get("username", ""),
            "password": form["unifi_protect"].get("password", ""),
            "verify_ssl": False,
        }

    if form.get("tuya_cloud", {}).get("access_id"):
        config["tuya"]["cloud"] = {
            "access_id": form["tuya_cloud"]["access_id"],
            "access_key": form["tuya_cloud"].get("access_key", ""),
            "api_region": form["tuya_cloud"].get("api_region") or "eu",
            "api_endpoint": form["tuya_cloud"].get("api_endpoint") or "https://openapi.tuyaeu.com",
            "mq_endpoint": form["tuya_cloud"].get("mq_endpoint") or "wss://mqe.tuyaeu.com:8285/",
            "camera_device_ids": [],
        }

    return config


def draft_path_for(config_path: pathlib.Path) -> pathlib.Path:
    return config_path.parent / DRAFT_FILENAME


def load_draft(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_draft(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def delete_draft(path: pathlib.Path) -> None:
    path.unlink(missing_ok=True)


CONFIG_HEADER = (
    "# Wygenerowane przez kreator pierwszej konfiguracji (/setup).\n"
    "# Ten plik jest w pełni edytowalny ręcznie - kreator NIE URUCHOMI SIĘ\n"
    "# PONOWNIE, dopóki ten plik istnieje (żeby nie nadpisać Twoich zmian).\n"
    "# Strefy/wyjścia/wejścia IP/partycje Satela są w satel_fields.yaml,\n"
    "# urządzenia Tuya w tuya_devices.yaml, sceny/reguły ruchu/symulacja\n"
    "# obecności w automations.yaml - wszystkie edytowalne z panelu webowego\n"
    "# (/config, /automations - wymaga hasła administratora do zapisu).\n"
    "# Pełny opis wszystkich pól, w tym tych pominiętych przez kreator\n"
    "# (reguły_kamera_alarm, alarmy progowe, automatyka kotła) - patrz\n"
    "# config.example.yaml.\n\n"
)

SENSORS_HEADER = (
    "# Lista czujników eBUS - dodaj je przez panel webowy (/config) po\n"
    "# uruchomieniu bridge'a, albo ręcznie - patrz sensors.example.yaml.\n\n"
)

AUTOMATIONS_HEADER = (
    "# Sceny / reguły ruchu (harmonogram + eskalacja) / symulacja obecności.\n"
    "# W pełni edytowalne z panelu webowego (/automations, wymaga hasła\n"
    "# administratora) - każdy zapis stamtąd NADPISUJE cały ten plik, więc\n"
    "# ręczne zmiany tutaj przetrwają tylko do następnego zapisu z panelu.\n"
    "# Opis pól - patrz config.example.yaml (sekcje scenes/reguły_ruchu/\n"
    "# presence_simulation) i README, sekcja \"Automatyzacje\".\n\n"
)

SATEL_FIELDS_HEADER = (
    "# Strefy / wyjścia / wejścia IP / partycje Satela - w pełni edytowalne\n"
    "# z panelu webowego (/config, wymaga hasła administratora do zapisu) -\n"
    "# każdy zapis stamtąd NADPISUJE cały ten plik. Połączenie z centralą\n"
    "# (host/port/kod użytkownika) zostaje w config.yaml.\n"
    "# Opis pól - patrz config.example.yaml (sekcja satel).\n\n"
)

TUYA_DEVICES_HEADER = (
    "# Urządzenia Tuya (lokalnie) - w pełni edytowalne z panelu webowego\n"
    "# (/config, wymaga hasła administratora do zapisu) - każdy zapis\n"
    "# stamtąd NADPISUJE cały ten plik. Konto/chmura Tuya (jeśli używana)\n"
    "# zostaje w config.yaml (sekcja tuya.cloud).\n"
    "# Opis pól - patrz config.example.yaml (sekcja tuya.devices).\n\n"
)
