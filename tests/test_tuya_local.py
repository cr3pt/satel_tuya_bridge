"""Testy dla bridge/tuya_local.py - retry przy chwilowej niedostępności
urządzeń (typowe dla tanich urządzeń WiFi w słabym zasięgu)."""
import pytest

from bridge.tuya_local import TuyaLocalController, scan_network, fetch_cloud_devices

DEVICES_CFG = {
    "gniazdko_salon": {"id": "dev1", "ip": "192.168.1.50", "local_key": "key1", "type": "socket"},
    "swiatlo_korytarz": {
        "id": "dev2", "ip": "192.168.1.51", "local_key": "key2", "type": "dimmer",
        "dimmer_dp": "3", "dimmer_range": [10, 1000],
    },
}


class FlakyDevice:
    """Symuluje urządzenie, które pada N razy z rzędu, a potem zaczyna działać -
    typowe zachowanie taniego urządzenia WiFi w słabym zasięgu."""

    def __init__(self, fail_times: int = 0, fail_via_error_dict: bool = False):
        self.fail_times = fail_times
        self.fail_via_error_dict = fail_via_error_dict
        self.call_count = 0
        self.turned_on = False

    def turn_on(self):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            if self.fail_via_error_dict:
                return {"Error": "Device not reachable"}  # tinytuya czasem nie rzuca wyjątku
            raise ConnectionError("symulowana awaria sieci")
        self.turned_on = True
        return {"dps": {"1": True}}

    def turn_off(self):
        self.turned_on = False
        return {"dps": {"1": False}}

    def status(self):
        return {"dps": {"1": self.turned_on}}

    def set_status(self, value, dp):
        return {"dps": {dp: value}}

    def set_value(self, dp, value):
        return {"dps": {dp: value}}


@pytest.fixture
def controller():
    ctrl = TuyaLocalController(DEVICES_CFG, retries=2, retry_delay=0.01)
    return ctrl


async def test_successful_call_on_first_try(controller):
    fake_device = FlakyDevice(fail_times=0)
    controller._devices["gniazdko_salon"] = fake_device
    await controller.turn_on("gniazdko_salon")
    assert fake_device.turned_on is True
    assert fake_device.call_count == 1


async def test_retries_after_transient_failure(controller):
    fake_device = FlakyDevice(fail_times=2)  # pada 2 razy, udaje się za 3.
    controller._devices["gniazdko_salon"] = fake_device
    await controller.turn_on("gniazdko_salon")
    assert fake_device.turned_on is True
    assert fake_device.call_count == 3


async def test_error_dict_response_triggers_retry(controller):
    """tinytuya czasem nie rzuca wyjątku przy awarii, tylko zwraca słownik
    z kluczem 'Error' - to też musi być traktowane jako błąd do ponowienia."""
    fake_device = FlakyDevice(fail_times=1, fail_via_error_dict=True)
    controller._devices["gniazdko_salon"] = fake_device
    await controller.turn_on("gniazdko_salon")
    assert fake_device.turned_on is True
    assert fake_device.call_count == 2


async def test_exhausted_retries_raises_connection_error(controller):
    fake_device = FlakyDevice(fail_times=10)  # pada więcej razy niż liczba prób
    controller._devices["gniazdko_salon"] = fake_device
    with pytest.raises(ConnectionError):
        await controller.turn_on("gniazdko_salon")
    assert fake_device.call_count == 3  # 1 próba + 2 ponowienia


async def test_turn_on_for_duration_still_turns_off_after_failed_turn_on(controller):
    """Jeśli turn_on ostatecznie się nie uda (wyczerpane próby), i tak
    powinniśmy spróbować wyłączyć po czasie - nie zostawiać niczego w
    niepewnym stanie."""
    fake_device = FlakyDevice(fail_times=10)
    controller._devices["gniazdko_salon"] = fake_device
    await controller.turn_on_for_duration("gniazdko_salon", duration_seconds=0.01)
    # turn_off powinien zostać wywołany mimo że turn_on się nie powiódł
    assert fake_device.turned_on is False


async def test_get_all_statuses_marks_unreachable_devices_as_none(controller):
    working = FlakyDevice(fail_times=0)
    broken = FlakyDevice(fail_times=999)
    controller._devices["gniazdko_salon"] = working
    controller._devices["swiatlo_korytarz"] = broken

    # get_status wywołuje `status()`, nie `turn_on()` - broken.status() nie
    # pada w tym mocku (tylko turn_on symuluje awarię), więc tu testujemy
    # ścieżkę przez wyjątek bezpośrednio w _call_with_retry
    class AlwaysFailingStatus:
        def status(self):
            raise ConnectionError("padło")

    controller._devices["swiatlo_korytarz"] = AlwaysFailingStatus()
    result = await controller.get_all_statuses()
    assert result["gniazdko_salon"] is not None
    assert result["swiatlo_korytarz"] is None


class TestScanNetwork:
    """scan_network() - podpowiedź id/ip/version urządzeń Tuya w panelu
    (/config), bez prawdziwej sieci LAN (tinytuya.deviceScan zamockowane)."""

    def test_returns_id_ip_version_for_each_discovered_device(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.deviceScan", lambda **kw: {
            "192.168.1.50": {"gwId": "dev1", "version": "3.4"},
            "192.168.1.51": {"id": "dev2", "version": "3.3"},
        })
        devices = scan_network()
        assert {"id": "dev1", "ip": "192.168.1.50", "version": "3.4"} in devices
        assert {"id": "dev2", "ip": "192.168.1.51", "version": "3.3"} in devices

    def test_no_devices_found_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.deviceScan", lambda **kw: {})
        assert scan_network() == []

    def test_maxretry_passed_through_to_tinytuya(self, monkeypatch):
        captured = {}

        def fake_scan(**kwargs):
            captured.update(kwargs)
            return {}

        monkeypatch.setattr("bridge.tuya_local.tinytuya.deviceScan", fake_scan)
        scan_network(maxretry=3)
        assert captured["maxretry"] == 3
        assert captured["poll"] is False  # nie mamy local_key przy skanowaniu - nie próbujemy odpytać dps


class FakeCloud:
    """Symuluje tinytuya.Cloud na tyle, ile potrzebuje fetch_cloud_devices()."""

    def __init__(self, devices=None, error=None, getdevices_raises=None, getdevices_raw=None, **kwargs):
        self.error = error
        self._devices = devices if devices is not None else []
        self._raises = getdevices_raises
        self.getdevices_raw = getdevices_raw

    def getdevices(self, *args, **kwargs):
        if self._raises:
            raise self._raises
        return self._devices


class TestFetchCloudDevices:
    """fetch_cloud_devices() - import urządzeń z Tuya Cloud (kreator/panel),
    alternatywa dla ręcznego wpisywania każdego urządzenia - bez prawdziwego
    konta Tuya (tinytuya.Cloud zamockowane)."""

    def test_returns_name_id_local_key_from_cloud(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(devices=[
            {"id": "d1", "name": "Gniazdko", "key": "localkey1"},
        ]))
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        devices = fetch_cloud_devices("eu", "access_id", "access_secret")
        assert devices == [{
            "id": "d1", "name": "Gniazdko", "local_key": "localkey1", "ip": "", "version": "",
            "gateway_id": "", "node_id": "",
        }]

    def test_surfaces_gateway_id_and_node_id_for_zigbee_sub_devices(self, monkeypatch):
        """Tuya Cloud dokłada gateway_id/node_id tylko dla podurządzeń
        Zigbee (patrz tinytuya Cloud.py: filter_devices/_update_device_list)
        - panel (/config) używa tego jako podpowiedzi, żeby dopasować
        sub-device do lokalnie nazwanej bramki."""
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(devices=[
            {"id": "sub1", "name": "Lampka Zigbee", "key": "", "gateway_id": "gw1", "node_id": "0011"},
        ]))
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        devices = fetch_cloud_devices("eu", "access_id", "access_secret")
        assert devices[0]["gateway_id"] == "gw1"
        assert devices[0]["node_id"] == "0011"

    def test_cross_references_lan_scan_by_id_for_ip(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(devices=[
            {"id": "d1", "name": "Gniazdko", "key": "localkey1"},
        ]))
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [
            {"id": "d1", "ip": "192.168.1.50", "version": "3.4"},
        ])
        devices = fetch_cloud_devices("eu", "access_id", "access_secret")
        assert devices[0]["ip"] == "192.168.1.50"
        assert devices[0]["version"] == "3.4"

    def test_lan_scan_failure_does_not_break_cloud_import(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(devices=[
            {"id": "d1", "name": "Gniazdko", "key": "localkey1"},
        ]))

        def raise_scan(**kwargs):
            raise RuntimeError("brak dostępu do sieci")

        monkeypatch.setattr("bridge.tuya_local.scan_network", raise_scan)
        devices = fetch_cloud_devices("eu", "access_id", "access_secret")
        assert devices[0]["ip"] == ""

    def test_login_error_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(error={"Error": "Cloud _gettoken() failed"}),
        )
        with pytest.raises(ConnectionError):
            fetch_cloud_devices("eu", "bad_id", "bad_secret")

    def test_getdevices_exception_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(getdevices_raises=KeyError("result")),
        )
        with pytest.raises(ConnectionError):
            fetch_cloud_devices("eu", "access_id", "access_secret")

    def test_getdevices_error_dict_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(devices={"Error": "token expired"}),
        )
        with pytest.raises(ConnectionError, match="token expired"):
            fetch_cloud_devices("eu", "access_id", "access_secret")

    def test_constructor_raising_is_wrapped_as_connection_error(self, monkeypatch):
        def raise_typeerror(**kwargs):
            raise TypeError("Tuya Cloud Key and Secret required")

        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", raise_typeerror)
        with pytest.raises(ConnectionError):
            fetch_cloud_devices("eu", "", "")

    def test_no_devices_found_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(devices=[]))
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        assert fetch_cloud_devices("eu", "access_id", "access_secret") == []

    def test_empty_result_with_success_false_raw_raises_clear_error(self, monkeypatch):
        """Regresja dla realnego bugu w tinytuya: gdy odpowiedź Tuya Cloud na
        listę urządzeń nie ma klucza 'result' (typowe dla błędnych danych
        logowania/regionu/uprawnień), tinytuya.Cloud._get_all_devices()
        cicho zostawia domyślne {'result': []} zamiast zgłosić błąd -
        getdevices() zwraca wtedy pustą listę NIEODRÓŻNIALNĄ od legalnego
        '0 urządzeń'. fetch_cloud_devices() musi to wykryć przez
        getdevices_raw i zgłosić czytelny błąd zamiast cichej pustej listy."""
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(devices=[], getdevices_raw={"success": False, "code": 1004, "msg": "sign invalid"}),
        )
        with pytest.raises(ConnectionError, match="sign invalid"):
            fetch_cloud_devices("eu", "access_id", "access_secret")

    def test_empty_result_without_success_false_flag_stays_empty_list(self, monkeypatch):
        """Legalny przypadek '0 urządzeń w projekcie' (success nieustawione
        na false w surowej odpowiedzi) NIE powinien być traktowany jak błąd."""
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(devices=[], getdevices_raw={"success": True, "result": []}),
        )
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        assert fetch_cloud_devices("eu", "access_id", "access_secret") == []

    def test_empty_result_missing_raw_attribute_stays_empty_list(self, monkeypatch):
        """getdevices_raw może nie istnieć wcale (np. starsza wersja
        tinytuya) - brak atrybutu nie może wywalić importu."""
        cloud = FakeCloud(devices=[])
        del cloud.getdevices_raw
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: cloud)
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        assert fetch_cloud_devices("eu", "access_id", "access_secret") == []


class TestZigbeeGateways:
    """Bramki Zigbee (Tuya) - czujniki/przełączniki Zigbee NIE łączą się
    bezpośrednio z WiFi, idą przez połączenie bramki (tinytuya:
    Device(cid=..., parent=<obiekt bramki>) - biblioteka sama kieruje ruch
    przez sesję TCP bramki, patrz XenonDevice.__init__/_send_receive)."""

    GATEWAYS_CFG = {
        "brama_salon": {"id": "gw1", "ip": "192.168.1.60", "local_key": "gwkey", "version": 3.4},
    }

    def test_gateway_device_created_from_cfg(self):
        ctrl = TuyaLocalController({}, self.GATEWAYS_CFG)
        assert "brama_salon" in ctrl._gateways
        assert ctrl._gateways["brama_salon"].id == "gw1"

    def test_sub_device_gets_cid_parent_and_zigbee_dev_type(self):
        devices_cfg = {
            "lampka": {"id": "sub1", "gateway": "brama_salon", "cid": "node1", "type": "socket"},
        }
        ctrl = TuyaLocalController(devices_cfg, self.GATEWAYS_CFG)
        dev = ctrl._devices["lampka"]
        assert dev.cid == "node1"
        assert dev.parent is ctrl._gateways["brama_salon"]
        assert dev.dev_type == "zigbee"

    def test_sub_device_cid_defaults_to_device_id_when_not_given(self):
        devices_cfg = {"lampka": {"id": "sub1", "gateway": "brama_salon", "type": "socket"}}
        ctrl = TuyaLocalController(devices_cfg, self.GATEWAYS_CFG)
        assert ctrl._devices["lampka"].cid == "sub1"

    def test_sub_device_inherits_gateway_protocol_version(self):
        """tinytuya ustawia to samo automatycznie w XenonDevice.__init__
        (XenonDevice.set_version(self, self.parent.version)) - test
        potwierdza, że faktycznie tak konstruujemy dziecko (parent=gateway),
        a nie osobno z niezależną wersją."""
        devices_cfg = {"lampka": {"id": "sub1", "gateway": "brama_salon", "type": "socket"}}
        ctrl = TuyaLocalController(devices_cfg, self.GATEWAYS_CFG)
        assert ctrl._devices["lampka"].version == ctrl._gateways["brama_salon"].version

    def test_device_without_gateway_field_is_a_plain_wifi_outlet(self):
        ctrl = TuyaLocalController(DEVICES_CFG, self.GATEWAYS_CFG)
        assert ctrl._devices["gniazdko_salon"].cid is None
        assert ctrl._devices["gniazdko_salon"].parent is None

    def test_dangling_gateway_reference_is_skipped_not_crashed(self):
        """Literówka w nazwie bramki (albo bramka jeszcze nie zapisana) nie
        może wywalić całego bridge'a przy starcie - reszta urządzeń musi
        dalej działać, ten jeden wiersz zostaje po prostu pominięty."""
        devices_cfg = {
            "lampka": {"id": "sub1", "gateway": "brama_nieistniejaca", "type": "socket"},
            "gniazdko_salon": DEVICES_CFG["gniazdko_salon"],
        }
        ctrl = TuyaLocalController(devices_cfg, self.GATEWAYS_CFG)
        assert "lampka" not in ctrl._devices
        assert "gniazdko_salon" in ctrl._devices

    def test_zigbee_gateways_cfg_defaults_to_empty_when_omitted(self):
        """Wsteczna zgodność - konstruktor wywoływany bez drugiego argumentu
        (jak przed dodaniem obsługi Zigbee) musi dalej działać."""
        ctrl = TuyaLocalController(DEVICES_CFG)
        assert ctrl._gateways == {}
        assert set(ctrl._devices) == set(DEVICES_CFG)
