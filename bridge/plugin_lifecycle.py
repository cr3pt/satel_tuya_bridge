# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

"""Zgodność działającego bridge'a z flagą `enabled` wtyczek wM-Bus i ESPHome.

`RulesEngine.start()` uruchamia nasłuch wM-Bus i połączenia ESPHome tylko raz,
przy starcie procesu. Bez tego modułu wyłączenie wtyczki w panelu zostawiało je
działające do pełnego restartu. `sync_plugin_lifecycle` jest wołane z
`RulesEngine.reload_config()` i doprowadza stan procesu do nowej konfiguracji:
zatrzymuje wyłączone komponenty, uruchamia włączone i restartuje te, których
ustawienia połączenia się zmieniły.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger("bridge.plugin_lifecycle")

WMBUS_HEALTH_KEY = "health:wmbus_mqtt"


def wmbus_listener_cfg(new_config) -> dict:
    wm = new_config.raw.get("wmbus") or {}
    ebus = getattr(new_config, "ebus", None) or {}
    return {
        "mqtt_host": wm.get("mqtt_host") or ebus.get("mqtt_host") or "127.0.0.1",
        "mqtt_port": wm.get("mqtt_port") or ebus.get("mqtt_port", 1883),
        "topic_prefix": wm.get("topic_prefix", "wmbusmeters"),
    }


def _make_wmbus_listener(engine, cfg: dict):
    from . import wmbus

    return wmbus.WmbusMqttListener(
        cfg, list(engine._wmbus_meters.values()), engine.handle_wmbus_reading,
        history=engine.history, notify_cfg=engine._notify_cfg,
        grace_seconds=engine._grace_seconds, loop=engine._loop,
    )


def _make_esphome_manager(engine, cfg: dict):
    from .esphome_client import EsphomeManager

    return EsphomeManager(
        cfg, engine._on_esphome_state, engine._on_esphome_entities,
        history=engine.history, notify_cfg=engine._notify_cfg, grace_seconds=engine._grace_seconds,
    )


def _set_wmbus_disabled_status(engine) -> None:
    try:
        engine.history.set_status(WMBUS_HEALTH_KEY, "system_health", "wyłączono (wtyczka)", label="wmbus_mqtt")
    except Exception:
        logger.exception("Nie zapisano statusu wyłączonej wtyczki wM-Bus")


def sync_wmbus(engine, new_config) -> str:
    """Zwraca: 'stopped', 'started', 'restarted' albo 'unchanged'."""
    wm = new_config.raw.get("wmbus") or {}
    want = bool(wm.get("enabled"))
    listener = engine.wmbus_listener
    cfg = wmbus_listener_cfg(new_config)

    if listener is not None and not want:
        listener.stop()
        engine.wmbus_listener = None
        _set_wmbus_disabled_status(engine)
        logger.info("Wtyczka wM-Bus wyłączona - zatrzymano nasłuch MQTT")
        return "stopped"
    if listener is None and want:
        engine.wmbus_listener = _make_wmbus_listener(engine, cfg)
        engine.wmbus_listener.start()
        logger.info("Wtyczka wM-Bus włączona - uruchomiono nasłuch MQTT")
        return "started"
    if listener is not None and want and getattr(listener, "cfg", None) != cfg:
        listener.stop()
        engine.wmbus_listener = _make_wmbus_listener(engine, cfg)
        engine.wmbus_listener.start()
        logger.info("Zmieniono ustawienia połączenia wM-Bus - ponownie uruchomiono nasłuch MQTT")
        return "restarted"
    return "unchanged"


def _find_run_forever_task(manager) -> asyncio.Task | None:
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        frame = getattr(coro, "cr_frame", None)
        if getattr(coro, "__name__", "") == "run_forever" and frame is not None \
                and frame.f_locals.get("self") is manager:
            return task
    return None


async def _stop_esphome(engine) -> None:
    manager = engine.esphome_manager
    engine.esphome_manager = None
    if manager is None:
        return
    task = getattr(engine, "_esphome_task", None) or _find_run_forever_task(manager)
    engine._esphome_task = None
    if task is not None and not task.done():
        # `run_forever` w bloku finally zatrzymuje każde urządzenie.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return
    for device in list(manager.devices.values()):
        try:
            await device.stop()
        except Exception:
            logger.exception("Nie zatrzymano urządzenia ESPHome '%s'", getattr(device, "name", "?"))


def _start_esphome(engine, cfg: dict) -> None:
    engine.esphome_manager = _make_esphome_manager(engine, cfg)
    engine._esphome_task = asyncio.create_task(engine.esphome_manager.run_forever())


async def sync_esphome(engine, new_config) -> str:
    cfg = getattr(new_config, "esphome", None) or {}
    want = bool(cfg) and cfg.get("enabled", True)
    manager = engine.esphome_manager

    if manager is not None and not want:
        await _stop_esphome(engine)
        engine._prune_esphome_health()
        logger.info("Wtyczka ESPHome wyłączona - zatrzymano połączenia z urządzeniami")
        return "stopped"
    if manager is None and want:
        _start_esphome(engine, cfg)
        engine._prune_esphome_health()
        logger.info("Wtyczka ESPHome włączona - uruchomiono połączenia z urządzeniami")
        return "started"
    if manager is not None and want and getattr(manager, "cfg", None) != cfg:
        await _stop_esphome(engine)
        _start_esphome(engine, cfg)
        engine._prune_esphome_health()
        logger.info("Zmieniono konfigurację ESPHome - ponownie uruchomiono połączenia")
        return "restarted"
    return "unchanged"


async def sync_plugin_lifecycle(engine, new_config) -> dict:
    """Wołane na końcu przeładowania konfiguracji. Błąd jednej wtyczki nie
    przerywa reszty przeładowania."""
    result: dict[str, str] = {}
    try:
        result["wmbus"] = sync_wmbus(engine, new_config)
    except Exception:
        logger.exception("Synchronizacja wtyczki wM-Bus nie powiodła się")
        result["wmbus"] = "error"
    try:
        result["esphome"] = await sync_esphome(engine, new_config)
    except Exception:
        logger.exception("Synchronizacja wtyczki ESPHome nie powiodła się")
        result["esphome"] = "error"
    return result
