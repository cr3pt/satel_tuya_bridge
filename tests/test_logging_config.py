import logging

from bridge.logging_config import PLUGIN_LOGGERS, apply_log_levels, plugin_config_section


class TestPluginLoggersMapping:

    def test_tuya_local_covers_both_local_and_cloud_loggers(self):
        # Wtyczki "tuya_zigbee"/"tuya_cloud" zostały scalone do jednej "tuya_local"
        # (jedna strona /plugins/tuya) - błędy komend do Tuya Cloud muszą trafiać
        # pod ten sam log_level co reszta Tuya, inaczej suwak poziomu logowania
        # na stronie Tuya (i przycisk "📜 Logi" na jej karcie) po cichu pomija
        # bridge.tuya_cloud.
        assert PLUGIN_LOGGERS["tuya_local"] == ["bridge.tuya_local", "bridge.tuya_cloud"]

    def test_no_orphaned_tuya_zigbee_or_tuya_cloud_plugin_ids(self):
        assert "tuya_zigbee" not in PLUGIN_LOGGERS
        assert "tuya_cloud" not in PLUGIN_LOGGERS

    def test_flow_engine_logger_is_registered(self):
        # Bez tego wpisu poziomu logowania Flow nie dało się w ogóle ustawić
        # (a logger bridge.flow_engine nigdy nie pojawiał się na liście
        # loggerów w Historii logów, bo nic go nie kontrolowało).
        assert PLUGIN_LOGGERS["flow_engine"] == ["bridge.flow_engine"]


class TestApplyLogLevels:

    def test_tuya_log_level_sets_both_local_and_cloud_loggers(self):
        config_raw = {"tuya": {"log_level": "DEBUG"}}
        apply_log_levels(config_raw)
        assert logging.getLogger("bridge.tuya_local").level == logging.DEBUG
        assert logging.getLogger("bridge.tuya_cloud").level == logging.DEBUG

    def test_falls_back_to_default_level_when_unset(self):
        config_raw = {"logging": {"default_level": "WARNING"}, "tuya": {}}
        apply_log_levels(config_raw)
        assert logging.getLogger("bridge.tuya_local").level == logging.WARNING
        assert logging.getLogger("bridge.tuya_cloud").level == logging.WARNING


class TestPluginConfigSection:

    def test_tuya_local_returns_whole_tuya_section(self):
        config_raw = {"tuya": {"log_level": "ERROR", "cloud": {"access_id": "x"}}}
        assert plugin_config_section(config_raw, "tuya_local") == config_raw["tuya"]

    def test_unknown_plugin_id_returns_empty_dict(self):
        assert plugin_config_section({}, "tuya_zigbee") == {}
        assert plugin_config_section({}, "tuya_cloud") == {}
