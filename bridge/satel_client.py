# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Awaitable

import aiohttp

from .resilience import Supervisor

logger = logging.getLogger("bridge.satel")

DEFAULT_VIRTUAL_INPUT_HTTP_PORT = 8888
DEFAULT_VIRTUAL_INPUT_URL_TEMPLATE = "http://{host}:{port}/{name}"

_RESULT_OK_CODES = (0x00, 0xFF)
_RESULT_ERROR_NAMES = {0x01: "nieznany kod użytkownika"}


def _describe_result_error(error_byte: int | None) -> str:
    if error_byte is None:
        return "brak danych w odpowiedzi"
    return _RESULT_ERROR_NAMES.get(error_byte, f"kod błędu 0x{error_byte:02X}")


def _patch_satel_integra_variable_length_bitmasks() -> None:
    from satel_integra.messages import SatelReadMessage
    from satel_integra.utils import decode_bitmask_le

    if getattr(SatelReadMessage.get_active_bits, "_bridge_patched", False):
        return

    warned_lengths: set[int] = set()

    def get_active_bits(self, expected_length: int) -> list[int]:
        actual_length = len(self.msg_data)
        if actual_length != expected_length:
            if actual_length not in warned_lengths:
                warned_lengths.add(actual_length)
                logger.warning(
                    "Satel: centrala przysłała bitmaskę o długości %s B zamiast "
                    "zakładanych przez bibliotekę %s B (najpewniej ten model centrali "
                    "obsługuje mniej stref/wyjść niż 256) - używam faktycznej długości "
                    "zamiast zgłaszać błąd (kolejne wystąpienia tej samej długości nie "
                    "będą już logowane, żeby nie zalewać logów)",
                    actual_length, expected_length,
                )
            expected_length = actual_length
        return decode_bitmask_le(self.msg_data, expected_length)

    get_active_bits._bridge_patched = True
    SatelReadMessage.get_active_bits = get_active_bits


async def _wait_for_command_result(client, action: str, msg, timeout: float = 5.0) -> None:
    try:
        result = await asyncio.wait_for(client._send_data_and_wait(msg), timeout=timeout)
    except asyncio.TimeoutError:
        raise ConnectionError(
            f"Centrala nie potwierdziła komendy ({action}) w ciągu {timeout:.0f}s - "
            f"sprawdź połączenie z centralą"
        )
    if result is None:
        raise ConnectionError(f"Centrala nie potwierdziła komendy ({action}) - brak odpowiedzi")
    error_byte = result.msg_data[0] if result.msg_data else None
    if error_byte not in _RESULT_OK_CODES:
        raise RuntimeError(f"Centrala odrzuciła komendę ({action}): {_describe_result_error(error_byte)}")


async def test_connection(host: str, port: int, timeout_seconds: float = 5.0,
                           encryption_key: str | None = None) -> None:
    from satel_integra import AsyncSatel
    _patch_satel_integra_variable_length_bitmasks()
    from satel_integra.exceptions import (
        SatelConnectFailedError, SatelConnectionInitializationError, SatelPanelBusyError,
    )

    client = AsyncSatel(host, port, integration_key=encryption_key)
    try:
        await asyncio.wait_for(client.connect(raise_exceptions=True), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise ConnectionError(
            f"Przekroczono czas oczekiwania ({timeout_seconds:.0f}s) na połączenie z {host}:{port}"
        )
    except SatelPanelBusyError:
        raise ConnectionError(
            f"Centrala {host}:{port} zajęta - inny klient integracji (DLOADX/GUARDX albo "
            "druga instancja bridge'a) jest już podłączony. Centrala Satel Integra obsługuje "
            "TYLKO JEDNEGO klienta integracji naraz."
        )
    except (SatelConnectFailedError, SatelConnectionInitializationError) as exc:
        raise ConnectionError(
            f"Nie udało się połączyć z {host}:{port} - sprawdź adres IP i port "
            f"(jeśli centrala ma włączone szyfrowanie, sprawdź też klucz szyfrowania): {exc}"
        )
    finally:
        try:
            await client.close()
        except Exception:
            pass


def _violated_zone_numbers(client) -> list[int]:
    return [number for number, state in client.state.zones.items() if state.violated is True]


def _active_output_numbers(client) -> list[int]:
    return [number for number, state in client.state.outputs.items() if state.active is True]


async def discover_active_zones_and_outputs(host: str, port: int, timeout_seconds: float = 60.0,
                                             encryption_key: str | None = None) -> dict:
    from satel_integra import AsyncSatel
    _patch_satel_integra_variable_length_bitmasks()
    from satel_integra.exceptions import (
        SatelConnectFailedError, SatelConnectionInitializationError, SatelPanelBusyError,
    )

    client = AsyncSatel(host, port, integration_key=encryption_key)
    seen_zones: set[int] = set()
    seen_outputs: set[int] = set()

    def _on_zone(_status: dict) -> None:
        seen_zones.update(_violated_zone_numbers(client))

    def _on_output(_status: dict) -> None:
        seen_outputs.update(_active_output_numbers(client))

    client.register_callbacks(zone_changed_callback=_on_zone, output_changed_callback=_on_output)

    try:
        await asyncio.wait_for(client.connect(raise_exceptions=True), timeout=5.0)
    except asyncio.TimeoutError:
        raise ConnectionError(f"Przekroczono czas oczekiwania na połączenie z {host}:{port}")
    except SatelPanelBusyError:
        raise ConnectionError(
            f"Centrala {host}:{port} zajęta - inny klient integracji już podłączony. Centrala "
            "Satel Integra obsługuje TYLKO JEDNEGO klienta integracji naraz. Zatrzymaj na chwilę "
            "kontener/proces bridge'a (docker compose stop bridge) i spróbuj ponownie."
        )
    except (SatelConnectFailedError, SatelConnectionInitializationError) as exc:
        raise ConnectionError(f"Nie udało się połączyć z {host}:{port} - sprawdź adres IP i port: {exc}")

    try:
        await client.start(enable_monitoring=True)
        await asyncio.sleep(timeout_seconds)
    finally:
        try:
            await client.close()
        except Exception:
            pass

    return {"zones": sorted(seen_zones), "outputs": sorted(seen_outputs)}


class BackgroundZoneOutputScan:

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._started_at: float | None = None
        self._duration_seconds: float = 0.0
        self._result: dict | None = None
        self._error: str | None = None

    def start(self, host: str, port: int, timeout_seconds: float = 30.0,
              encryption_key: str | None = None) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("Nasłuch już trwa - poczekaj na koniec i spróbuj ponownie")
            self._running = True
            self._started_at = time.time()
            self._duration_seconds = timeout_seconds
            self._result = None
            self._error = None

        def _run() -> None:
            try:
                result = asyncio.run(discover_active_zones_and_outputs(
                    host, port, timeout_seconds=timeout_seconds, encryption_key=encryption_key,
                ))
            except ImportError:
                error = "Biblioteka satel_integra nie jest zainstalowana na serwerze"
                result = None
            except ConnectionError as exc:
                error = str(exc)
                result = None
            except Exception as exc:
                error = f"Błąd wykrywania: {exc}"
                result = None
            else:
                error = None
            with self._lock:
                self._result = result
                self._error = error
                self._running = False

        threading.Thread(target=_run, daemon=True).start()

    def status(self) -> dict:
        with self._lock:
            elapsed = (time.time() - self._started_at) if self._started_at is not None else 0.0
            return {
                "running": self._running,
                "elapsed_seconds": round(elapsed, 1),
                "duration_seconds": self._duration_seconds,
                "result": self._result,
                "error": self._error,
            }


async def test_output(host: str, port: int, integration_key: str, output_number: int,
                       state: bool = True, timeout_seconds: float = 5.0,
                       encryption_key: str | None = None) -> None:
    from satel_integra import AsyncSatel
    _patch_satel_integra_variable_length_bitmasks()
    from satel_integra.commands import SatelWriteCommand
    from satel_integra.exceptions import (
        SatelConnectFailedError, SatelConnectionInitializationError, SatelPanelBusyError,
    )
    from satel_integra.messages import SatelWriteMessage

    client = AsyncSatel(host, port, integration_key=encryption_key)
    try:
        await asyncio.wait_for(client.connect(raise_exceptions=True), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise ConnectionError(
            f"Przekroczono czas oczekiwania ({timeout_seconds:.0f}s) na połączenie z {host}:{port}"
        )
    except SatelPanelBusyError:
        raise ConnectionError(
            f"Centrala {host}:{port} zajęta - inny klient integracji już podłączony."
        )
    except (SatelConnectFailedError, SatelConnectionInitializationError) as exc:
        raise ConnectionError(f"Nie udało się połączyć z {host}:{port} - sprawdź adres IP i port: {exc}")

    try:
        await client.start(enable_monitoring=False)
        mode_command = SatelWriteCommand.OUTPUTS_ON if state else SatelWriteCommand.OUTPUTS_OFF
        msg = SatelWriteMessage(mode_command, code=integration_key, zones_or_outputs=[output_number])
        await _wait_for_command_result(client, f"test wyjścia {output_number} -> {state}", msg,
                                        timeout=timeout_seconds)
        logger.info("Test wyjścia %s -> %s zakończony sukcesem", output_number, state)
    finally:
        try:
            await client.close()
        except Exception:
            pass


class SatelVirtualInputTrigger:

    def __init__(self, host: str, cfg: dict):
        self.host = host
        self.port = cfg.get("port", DEFAULT_VIRTUAL_INPUT_HTTP_PORT)
        self.url_template = DEFAULT_VIRTUAL_INPUT_URL_TEMPLATE
        self.inputs = cfg.get("inputs", {})

    async def trigger(self, input_name: str, retries: int = 1) -> None:
        if input_name not in self.inputs:
            logger.error("Nieznane wirtualne wejście IP (tryb http): %s", input_name)
            return
        url = self.url_template.format(host=self.host, port=self.port, name=input_name)
        for attempt in range(retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        await resp.read()
                logger.info(
                    "Wysłano naruszenie wirtualnego wejścia '%s' (tryb http)",
                    input_name,
                )
                return
            except asyncio.TimeoutError:
                logger.warning(
                    "Timeout przy wywołaniu wejścia IP '%s' (próba %s/%s) - ETHM-1 Plus może "
                    "mimo to zarejestrować zdarzenie (brak potwierdzenia to znana cecha).",
                    input_name, attempt + 1, retries + 1,
                )
                return
            except aiohttp.ClientError as exc:
                if attempt < retries:
                    logger.warning(
                        "Błąd sieci przy wywołaniu wejścia IP '%s' (próba %s/%s): %s - ponawiam",
                        input_name, attempt + 1, retries + 1, exc,
                    )
                    await asyncio.sleep(1)
                else:
                    logger.error(
                        "Błąd sieci przy wywołaniu wejścia IP '%s' - wyczerpano próby: %s",
                        input_name, exc,
                    )


async def test_virtual_input_http(host: str, port: int, url_template: str, name: str,
                                   timeout_seconds: float = 5.0) -> None:
    url = url_template.format(host=host, port=port, name=name)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                await resp.read()
    except asyncio.TimeoutError:
        raise ConnectionError(f"Przekroczono czas oczekiwania ({timeout_seconds:.0f}s) na {host}:{port}")
    except aiohttp.ClientError as exc:
        raise ConnectionError(f"Błąd sieci przy wywołaniu wejścia IP: {exc}")


class SatelIntegraClient:

    def __init__(self, host: str, port: int, integration_key: str, partition: int = 1,
                 monitored_zones: dict | None = None, monitored_outputs: dict | None = None,
                 virtual_inputs: dict | None = None, encryption_key: str | None = None,
                 history=None, notify_cfg: dict | None = None, grace_seconds: float = 120,
                 status_poll_interval_seconds: float = 3.0):
        self.host = (host or "").strip()
        self.port = port
        self.integration_key = (integration_key or "").strip()
        self.encryption_key = (encryption_key or "").strip() or None
        self.partition = partition
        self.monitored_zones = monitored_zones or {}
        self.monitored_outputs = monitored_outputs or {}
        self.virtual_inputs = virtual_inputs or {}
        self._client = None
        self._zone_cb = None
        self._output_cb = None
        self._alarm_status_cb = None
        self._own_output_ts: dict[int, float] = {}
        self._reading_loop_alive = True
        self._supervisor = Supervisor("satel", history=history, notify_cfg=notify_cfg, grace_seconds=grace_seconds)
        self.status_poll_interval_seconds = status_poll_interval_seconds
        self._initial_status_delay_seconds = 1.0

    async def connect(self) -> None:
        from satel_integra import AsyncSatel, AlarmState
        _patch_satel_integra_variable_length_bitmasks()
        from satel_integra.exceptions import (
            SatelConnectFailedError, SatelConnectionInitializationError, SatelPanelBusyError,
        )

        self._AlarmState = AlarmState
        self._client = AsyncSatel(
            self.host,
            self.port,
            monitored_zones=list(self.monitored_zones.values()),
            monitored_outputs=list(self.monitored_outputs.values()),
            integration_key=self.encryption_key,
        )
        self._client.register_callbacks(
            alarm_status_callback=self._alarm_status_cb,
            zone_changed_callback=self._zone_cb,
            output_changed_callback=self._output_cb,
        )
        try:
            await self._client.connect(raise_exceptions=True)
        except SatelPanelBusyError as exc:
            raise ConnectionError(
                f"Centrala {self.host}:{self.port} zajęta - najprawdopodobniej inny klient "
                "integracji (DLOADX/GUARDX albo druga instancja bridge'a) jest już podłączony. "
                "Centrala Satel Integra obsługuje TYLKO JEDNEGO klienta integracji naraz."
            ) from exc
        except (SatelConnectFailedError, SatelConnectionInitializationError) as exc:
            raise ConnectionError(
                f"Nie udało się połączyć z Satelem pod {self.host}:{self.port} "
                f"(jeśli centrala ma włączone szyfrowanie, sprawdź też klucz szyfrowania): {exc}"
            ) from exc
        logger.info("Połączono z centralą Satel Integra (%s:%s)", self.host, self.port)

        self._reading_loop_alive = True
        original_reading_loop = self._client._reading_loop

        async def _reading_loop_with_watchdog():
            try:
                await original_reading_loop()
            finally:
                self._reading_loop_alive = False

        self._client._reading_loop = _reading_loop_with_watchdog

        await self._client.start(enable_monitoring=True)

        asyncio.ensure_future(self._periodic_status_sync())
        return

    async def _periodic_status_sync(self) -> None:
        from satel_integra.commands import SatelReadCommand
        from satel_integra.messages import SatelWriteMessage

        await asyncio.sleep(self._initial_status_delay_seconds)
        client = self._client
        while client is not None and client is self._client and not getattr(client, "stopped", True):
            try:
                if self.monitored_zones:
                    await client._send_data(SatelWriteMessage(SatelReadCommand.ZONES_VIOLATED))
                if self.monitored_outputs:
                    await client._send_data(SatelWriteMessage(SatelReadCommand.OUTPUTS_STATE))
            except Exception:
                logger.exception("Nie udało się dopytać o stan wejść/wyjść Satela - ponowię próbę")
            await asyncio.sleep(self.status_poll_interval_seconds)

    def recent_own_output(self, output_number: int, within_seconds: float) -> bool:
        ts = self._own_output_ts.get(output_number)
        return ts is not None and time.monotonic() - ts <= within_seconds

    async def set_output(self, output_number: int, state: bool) -> None:
        if self._client is None:
            raise RuntimeError("Klient Satel nie jest połączony - wywołaj connect() najpierw")
        if not self.integration_key:
            raise RuntimeError(
                "Sterowanie wymaga klucza integracji Satela - ustaw go w zakładce "
                "⚙ Ustawienia -> 🔌 Połączenia (bez klucza panel działa tylko do odczytu)."
            )
        from satel_integra.commands import SatelWriteCommand
        from satel_integra.messages import SatelWriteMessage

        self._own_output_ts[output_number] = time.monotonic()
        mode_command = SatelWriteCommand.OUTPUTS_ON if state else SatelWriteCommand.OUTPUTS_OFF
        msg = SatelWriteMessage(mode_command, code=self.integration_key, zones_or_outputs=[output_number])
        await _wait_for_command_result(self._client, f"wyjście {output_number} -> {state}", msg)
        logger.info("Ustawiono wyjście %s na %s", output_number, state)

    async def arm(self, mode: int = 0, partitions: list[int] | None = None) -> None:
        if self._client is None:
            raise RuntimeError("Klient Satel nie jest połączony")
        if not self.integration_key:
            raise RuntimeError(
                "Sterowanie wymaga klucza integracji Satela - ustaw go w zakładce "
                "⚙ Ustawienia -> 🔌 Połączenia (bez klucza panel działa tylko do odczytu)."
            )
        from satel_integra.commands import SatelWriteCommand
        from satel_integra.messages import SatelWriteMessage

        partitions = partitions or [self.partition]
        mode_command = SatelWriteCommand(SatelWriteCommand.PARTITIONS_ARM_MODE_0 + mode)
        msg = SatelWriteMessage(mode_command, code=self.integration_key, partitions=partitions)
        await _wait_for_command_result(self._client, f"uzbrojenie stref {partitions} (tryb {mode})", msg)
        logger.info("Uzbrojono strefy %s (tryb %s)", partitions, mode)

    async def disarm(self, partitions: list[int] | None = None) -> None:
        if self._client is None:
            raise RuntimeError("Klient Satel nie jest połączony")
        if not self.integration_key:
            raise RuntimeError(
                "Sterowanie wymaga klucza integracji Satela - ustaw go w zakładce "
                "⚙ Ustawienia -> 🔌 Połączenia (bez klucza panel działa tylko do odczytu)."
            )
        from satel_integra.commands import SatelWriteCommand
        from satel_integra.messages import SatelWriteMessage

        partitions = partitions or [self.partition]
        clear_alarm_necessary = bool(self.triggered_partition_numbers & set(partitions))
        msg = SatelWriteMessage(SatelWriteCommand.PARTITIONS_DISARM, code=self.integration_key, partitions=partitions)
        await _wait_for_command_result(self._client, f"rozbrojenie stref {partitions}", msg)
        logger.info("Rozbrojono strefy %s", partitions)
        if clear_alarm_necessary:
            await asyncio.sleep(1)
            msg2 = SatelWriteMessage(
                SatelWriteCommand.PARTITIONS_CLEAR_ALARM, code=self.integration_key, partitions=partitions,
            )
            await _wait_for_command_result(self._client, f"czyszczenie pamięci alarmu stref {partitions}", msg2)
            logger.info("Wyczyszczono pamięć alarmu dla stref %s", partitions)

    def _schedule(self, coro) -> None:
        task = asyncio.ensure_future(coro)

        def _log_exception(t: "asyncio.Task") -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.error("Błąd w callbacku Satela", exc_info=t.exception())

        task.add_done_callback(_log_exception)

    def register_zone_callback(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        def _sync_wrapper(status: dict) -> None:
            self._schedule(callback(status))

        self._zone_cb = _sync_wrapper
        if self._client is not None:
            self._client.register_callbacks(zone_changed_callback=_sync_wrapper)

    def register_output_callback(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        def _sync_wrapper(status: dict) -> None:
            self._schedule(callback(status))

        self._output_cb = _sync_wrapper
        if self._client is not None:
            self._client.register_callbacks(output_changed_callback=_sync_wrapper)

    def register_alarm_status_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        def _sync_wrapper() -> None:
            self._schedule(callback())

        self._alarm_status_cb = _sync_wrapper
        if self._client is not None:
            self._client.register_callbacks(alarm_status_callback=_sync_wrapper)

    @property
    def violated_zones(self) -> list[int]:
        return _violated_zone_numbers(self._client) if self._client else []

    @property
    def violated_outputs(self) -> list[int]:
        return _active_output_numbers(self._client) if self._client else []

    @property
    def armed_partition_numbers(self) -> set[int]:
        if self._client is None:
            return set()
        numbers: set[int] = set()
        for partitions in self._client.state.partition_states.values():
            numbers.update(partitions)
        return numbers

    @property
    def triggered_partition_numbers(self) -> set[int]:
        if self._client is None:
            return set()
        from satel_integra import AlarmState
        numbers: set[int] = set()
        for state in (AlarmState.TRIGGERED, AlarmState.TRIGGERED_FIRE):
            numbers.update(self._client.state.partition_states.get(state, []))
        return numbers

    @property
    def is_connected(self) -> bool:
        return (
            self._client is not None
            and not getattr(self._client, "stopped", True)
            and getattr(self, "_reading_loop_alive", True)
        )

    async def run_forever(self) -> None:

        async def _connect_and_monitor(on_connected) -> None:
            await self.connect()
            await on_connected()
            client = self._client
            while (
                client is not None
                and client is self._client
                and not getattr(client, "stopped", True)
                and self._reading_loop_alive
            ):
                await asyncio.sleep(5)

        await self._supervisor.run(_connect_and_monitor)
