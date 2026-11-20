# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aioesphomeapi import (
    APIClient,
    APIConnectionError,
    ButtonInfo,
    EntityInfo,
    EntityState,
    LightInfo,
    ReconnectLogic,
    SwitchInfo,
)

from .resilience import HealthReporter

logger = logging.getLogger("bridge.esphome")

_DEFAULT_PORT = 6053

# `OnState` dostaje nazwę urządzenia, EntityInfo encji (obiekt_id/name/key) i
# zdekodowaną wartość (bool/float/str - w zależności od typu encji, patrz
# aioesphomeapi.model: SensorState.state=float, BinarySensorState.state=bool,
# TextSensorState.state=str, SwitchState.state=bool, LightState.state=bool).
OnState = Callable[[str, EntityInfo, Any], Awaitable[None]]
# `OnEntities` dostaje nazwę urządzenia i pełną listę jego encji - wywoływane
# raz po każdym (re)połączeniu, PRZED pierwszymi stanami, bo przyciski
# (`ButtonInfo`) nigdy nie wysyłają stanu (nie mają odpowiadającej klasy
# EntityState) - bez tego wywołania panel nigdy by się o nich nie dowiedział.
OnEntities = Callable[[str, "list[EntityInfo]"], Awaitable[None]]


class EsphomeError(Exception):
    pass


@dataclass
class _DeviceConfig:
    name: str
    host: str
    port: int = _DEFAULT_PORT
    password: str | None = None
    noise_psk: str | None = None
    label: str = field(default="")

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.name


def _normalize_device(name: str, raw: dict) -> _DeviceConfig:
    return _DeviceConfig(
        name=name,
        host=(raw.get("host") or "").strip(),
        port=int(raw.get("port") or _DEFAULT_PORT),
        password=raw.get("password") or None,
        noise_psk=raw.get("noise_psk") or raw.get("api_key") or None,
        label=(raw.get("label") or "").strip(),
    )


class EsphomeDeviceClient:
    """Trzyma natywne (binarne, protobuf) połączenie z JEDNYM urządzeniem
    ESPHome przez `aioesphomeapi` - w odróżnieniu od reszty integracji
    (iQua/HomeWhiz/LG ThinQ), to NIE jest zwykłe REST/cloud API, więc nie
    korzystamy z `Supervisor` (ten zakłada własną pętlę ponawiania połączeń w
    `connect_and_run`) - `aioesphomeapi.ReconnectLogic` już implementuje
    dokładnie to samo (narastające odstępy między próbami + nasłuch mDNS do
    szybszego wykrycia, że urządzenie znowu widać w sieci), więc
    `HealthReporter` podłączamy wprost pod jego `on_connect`/`on_disconnect`,
    tak jak `WmbusMqttListener` (bridge/wmbus.py) robi to dla paho-mqtt."""

    def __init__(
        self, cfg: dict, on_state: OnState, on_entities: OnEntities | None = None,
        history=None, notify_cfg: dict | None = None, grace_seconds: float = 120,
    ):
        name = cfg["name"]
        self._cfg = _normalize_device(name, cfg)
        self.name = name
        self.label = self._cfg.label
        self.on_state = on_state
        self.on_entities = on_entities
        self._health = HealthReporter(f"esphome_{name}", history=history, notify_cfg=notify_cfg, grace_seconds=grace_seconds)
        self._entities_by_key: dict[int, EntityInfo] = {}
        self._connected = False
        self._stopping = False
        self._history = history
        self._client = APIClient(
            self._cfg.host, self._cfg.port,
            password=self._cfg.password, noise_psk=self._cfg.noise_psk,
        )
        self._reconnect = ReconnectLogic(
            client=self._client,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            on_connect_error=self._on_connect_error,
            name=name,
        )

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def entities(self) -> list[EntityInfo]:
        return list(self._entities_by_key.values())

    async def start(self) -> None:
        """Planuje pierwszą próbę połączenia i wraca - `ReconnectLogic`
        zarządza dalej samodzielnie w tle (nie jest to pętla blokująca)."""
        await self._reconnect.start()

    async def stop(self) -> None:
        """Celowe zatrzymanie (wyłączenie/restart) - to nie awaria, więc bez
        statusu "rozłączono" w panelu ani powiadomienia."""
        self._stopping = True
        await self._reconnect.stop()
        await self._client.disconnect()
        if self._history is not None:
            self._history.delete_status(f"health:esphome_{self.name}")

    async def _on_connect(self) -> None:
        try:
            entities, _services = await self._client.list_entities_services()
        except APIConnectionError as exc:
            logger.warning("ESPHome '%s': błąd pobierania listy encji - %s", self.name, exc)
            raise
        self._entities_by_key = {e.key: e for e in entities}
        if self.on_entities:
            await self.on_entities(self.name, entities)
        self._client.subscribe_states(self._on_state_change)
        self._connected = True
        self._health.report(True)
        logger.info("ESPHome '%s': połączono, %d encji", self.name, len(entities))

    async def _on_disconnect(self, expected: bool) -> None:
        self._connected = False
        if self._stopping:
            return
        self._health.report(False, "rozłączono" if not expected else "rozłączono (oczekiwane)")

    async def _on_connect_error(self, exc: Exception) -> None:
        self._connected = False
        self._health.report(False, str(exc))

    def _on_state_change(self, state: EntityState) -> None:
        info = self._entities_by_key.get(state.key)
        if info is None:
            return
        if getattr(state, "missing_state", False):
            return
        if not hasattr(state, "state"):
            return
        asyncio.create_task(self.on_state(self.name, info, state.state))

    def _entity_by_object_id(self, object_id: str) -> EntityInfo | None:
        for info in self._entities_by_key.values():
            if info.object_id == object_id:
                return info
        return None

    def switch_set(self, object_id: str, state: bool) -> None:
        info = self._entity_by_object_id(object_id)
        if not isinstance(info, SwitchInfo):
            raise EsphomeError(f"ESPHome '{self.name}': brak przełącznika '{object_id}'")
        if not self._connected:
            raise EsphomeError(f"ESPHome '{self.name}': urządzenie nie jest połączone")
        self._client.switch_command(info.key, state)

    def light_set(self, object_id: str, state: bool, brightness: float | None = None) -> None:
        info = self._entity_by_object_id(object_id)
        if not isinstance(info, LightInfo):
            raise EsphomeError(f"ESPHome '{self.name}': brak światła '{object_id}'")
        if not self._connected:
            raise EsphomeError(f"ESPHome '{self.name}': urządzenie nie jest połączone")
        self._client.light_command(info.key, state=state, brightness=brightness)

    def button_press(self, object_id: str) -> None:
        info = self._entity_by_object_id(object_id)
        if not isinstance(info, ButtonInfo):
            raise EsphomeError(f"ESPHome '{self.name}': brak przycisku '{object_id}'")
        if not self._connected:
            raise EsphomeError(f"ESPHome '{self.name}': urządzenie nie jest połączone")
        self._client.button_command(info.key)


class EsphomeManager:
    """Zarządza wszystkimi skonfigurowanymi urządzeniami ESPHome (każde ma
    własne, niezależne połączenie - w odróżnieniu od np. iQua/HomeWhiz, gdzie
    jeden klient odpytuje wiele urządzeń przez jedno konto w chmurze)."""

    def __init__(
        self, cfg: dict, on_state: OnState, on_entities: OnEntities | None = None,
        history=None, notify_cfg: dict | None = None, grace_seconds: float = 120,
    ):
        self.cfg = cfg
        self._devices: dict[str, EsphomeDeviceClient] = {}
        for name, raw in (cfg.get("devices") or {}).items():
            if not raw.get("enabled", True) or not (raw.get("host") or "").strip():
                continue
            device_cfg = dict(raw)
            device_cfg["name"] = name
            self._devices[name] = EsphomeDeviceClient(
                device_cfg, on_state, on_entities,
                history=history, notify_cfg=notify_cfg, grace_seconds=grace_seconds,
            )

    def get_device(self, name: str) -> EsphomeDeviceClient | None:
        return self._devices.get(name)

    @property
    def devices(self) -> dict[str, EsphomeDeviceClient]:
        return self._devices

    async def run_forever(self) -> None:
        if not self._devices:
            logger.info("ESPHome: brak skonfigurowanych urządzeń")
            return
        logger.info("ESPHome: łączę z %d urządzeniami", len(self._devices))
        for device in self._devices.values():
            await device.start()
        try:
            await asyncio.Event().wait()
        finally:
            for device in self._devices.values():
                await device.stop()
