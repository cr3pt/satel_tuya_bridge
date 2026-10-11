# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import shutil
import sys

import pytest

import plugins


PLUGINS_DIR = plugins._PACKAGE_DIR


@pytest.fixture
def temp_plugin_dir():
    created = []

    def _make(name: str, routes_py: str):
        plugin_dir = PLUGINS_DIR / name
        plugin_dir.mkdir()
        (plugin_dir / "routes.py").write_text(routes_py, encoding="utf-8")
        created.append(name)
        return plugin_dir

    yield _make

    for name in created:
        module_name = f"plugins.{name}.routes"
        sys.modules.pop(module_name, None)
        shutil.rmtree(PLUGINS_DIR / name, ignore_errors=True)


class TestDiscoverPluginMetas:

    def test_discovers_all_real_plugins_with_no_errors(self):
        metas, errors = plugins.discover_plugin_metas()
        assert errors == []
        ids = {m.id for m in metas}
        assert ids == {
            "satel", "tuya_local", "ebus", "home_assistant",
            "mqtt_publish", "unifi_protect", "homewhiz", "lg_thinq", "iqua", "smartthings",
            "presence_simulation", "notifications", "mcp_server", "homekit", "energy_cost", "wmbus",
        }

    def test_all_real_plugins_have_describe_state(self):
        metas, _errors = plugins.discover_plugin_metas()
        for meta in metas:
            assert meta.describe_state is not None, meta.id
            state = meta.describe_state({})
            assert {"enabled", "configured", "detail"} <= set(state)

    def test_missing_register_function_is_reported(self, temp_plugin_dir):
        temp_plugin_dir("zz_test_missing_register", "x = 1\n")
        _metas, errors = plugins.discover_plugin_metas()
        by_dir = {e.plugin_dir: e.error for e in errors}
        assert "zz_test_missing_register" in by_dir
        assert "register" in by_dir["zz_test_missing_register"]

    def test_invalid_meta_type_is_reported(self, temp_plugin_dir):
        temp_plugin_dir(
            "zz_test_bad_meta",
            "def register(app, **deps):\n    return None\nMETA = 'not a PluginMeta'\n",
        )
        _metas, errors = plugins.discover_plugin_metas()
        by_dir = {e.plugin_dir: e.error for e in errors}
        assert "zz_test_bad_meta" in by_dir

    def test_import_error_is_reported(self, temp_plugin_dir):
        temp_plugin_dir("zz_test_import_error", "raise RuntimeError('boom')\n")
        _metas, errors = plugins.discover_plugin_metas()
        by_dir = {e.plugin_dir: e.error for e in errors}
        assert "zz_test_import_error" in by_dir
        assert "boom" in by_dir["zz_test_import_error"]

    def test_broken_plugin_does_not_prevent_real_plugins_from_loading(self, temp_plugin_dir):
        temp_plugin_dir("zz_test_broken_alongside_real", "raise RuntimeError('boom')\n")
        metas, errors = plugins.discover_plugin_metas()
        assert len(metas) == 16
        assert any(e.plugin_dir == "zz_test_broken_alongside_real" for e in errors)

    def test_underscore_prefixed_dirs_are_skipped_silently(self):
        hidden_dir = PLUGINS_DIR / "_zz_test_hidden"
        hidden_dir.mkdir()
        try:
            (hidden_dir / "routes.py").write_text("raise RuntimeError('should not run')\n", encoding="utf-8")
            _metas, errors = plugins.discover_plugin_metas()
            assert not any(e.plugin_dir == "_zz_test_hidden" for e in errors)
        finally:
            shutil.rmtree(hidden_dir, ignore_errors=True)

    def test_directory_without_routes_py_is_ignored(self):
        plain_dir = PLUGINS_DIR / "zz_test_no_routes"
        plain_dir.mkdir()
        try:
            (plain_dir / "notes.txt").write_text("not a plugin", encoding="utf-8")
            _metas, errors = plugins.discover_plugin_metas()
            assert not any(e.plugin_dir == "zz_test_no_routes" for e in errors)
        finally:
            shutil.rmtree(plain_dir, ignore_errors=True)


class TestRegisterAll:

    def test_register_all_skips_broken_plugin_and_registers_real_ones(self, temp_plugin_dir):
        temp_plugin_dir("zz_test_register_boom", "raise RuntimeError('boom')\n")

        from quart import Quart
        app = Quart(__name__)
        metas, errors = plugins.register_all(
            app,
            require_auth=lambda **_kw: (lambda fn: fn),
            get_auth_guard=lambda: None,
            get_config_raw=lambda: {},
            get_config_store=lambda: None,
            get_config_path=lambda: "config.db",
            reload_bridge_config=lambda: None,
            enqueue_bridge_reload=lambda: None,
            get_history=lambda: None,
            get_satel_partitions=lambda: {},
        )
        assert len(metas) == 16
        assert any(e.plugin_dir == "zz_test_register_boom" for e in errors)

    def test_register_all_reports_error_when_register_itself_raises(self, temp_plugin_dir):
        temp_plugin_dir(
            "zz_test_register_raises",
            "from plugins import PluginMeta\n"
            "def describe_state(config_raw):\n"
            "    return {'enabled': False, 'configured': False, 'detail': ''}\n"
            "META = PluginMeta(id='zz_test_register_raises', name='x', icon='x', description='x', "
            "describe_state=describe_state)\n"
            "def register(app, **deps):\n"
            "    raise RuntimeError('register boom')\n",
        )
        from quart import Quart
        app = Quart(__name__)
        metas, errors = plugins.register_all(
            app,
            require_auth=lambda **_kw: (lambda fn: fn),
            get_auth_guard=lambda: None,
            get_config_raw=lambda: {},
            get_config_store=lambda: None,
            get_config_path=lambda: "config.db",
            reload_bridge_config=lambda: None,
            enqueue_bridge_reload=lambda: None,
            get_history=lambda: None,
            get_satel_partitions=lambda: {},
        )
        ids = {m.id for m in metas}
        assert "zz_test_register_raises" not in ids
        by_dir = {e.plugin_dir: e.error for e in errors}
        assert "register boom" in by_dir["zz_test_register_raises"]
