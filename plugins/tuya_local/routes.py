from __future__ import annotations

import asyncio
import ipaddress

from quart import Quart, jsonify, redirect, render_template, request

from bridge.tuya_local import (
    TUYA_CLOUD_REGIONS,
    scan_network as scan_tuya_network,
    fetch_cloud_devices as fetch_tuya_cloud_devices,
    fetch_dp_schemas_for_devices,
    test_local_device_status,
)
from dashboard.setup import (
    build_tuya_devices_dict, validate_tuya_devices, validate_cloud_devices,
    tuya_devices_to_rows, zigbee_gateways_to_rows, cloud_devices_to_rows,
    validate_cloud_accounts, build_cloud_accounts_dict, cloud_accounts_to_rows,
)
from plugins import PluginMeta, register_settings_route


def _zigbee_devices(config_raw: dict) -> dict:
    tuya_devices = (config_raw.get("tuya", {}) or {}).get("devices", {}) or {}
    return {name: cfg for name, cfg in tuya_devices.items() if cfg.get("gateway")}


def describe_state(config_raw: dict) -> dict:
    tuya = config_raw.get("tuya", {}) or {}
    tuya_devices = tuya.get("devices", {}) or {}
    zigbee_devices = _zigbee_devices(config_raw)
    cloud_devices = tuya.get("cloud_devices", {}) or {}
    cloud_accounts = tuya.get("cloud_accounts", {}) or {}
    detail_parts = [f"{len(tuya_devices) - len(zigbee_devices)} WiFi"]
    if zigbee_devices:
        detail_parts.append(f"{len(zigbee_devices)} Zigbee")
    if cloud_devices:
        account_note = f" ({len(cloud_accounts)} kont)" if len(cloud_accounts) > 1 else ""
        detail_parts.append(f"{len(cloud_devices)} chmurowych{account_note}")
    has_cloud_account = any(acc.get("access_id") for acc in cloud_accounts.values())
    return {
        "enabled": tuya.get("enabled", True) and (len(tuya_devices) > len(zigbee_devices) or has_cloud_account),
        "configured": len(tuya_devices) > len(zigbee_devices) or has_cloud_account,
        "detail": ", ".join(detail_parts),
        "poll_interval_seconds": tuya.get("status_poll_interval_seconds", 30),
    }


META = PluginMeta(
    id="tuya_local",
    name="Tuya (LAN + Cloud + Zigbee)",
    icon="🔌",
    description="Wszystko związane z Tuya w jednym miejscu: gniazdka/przełączniki/ściemniacze sterowane "
                "bezpośrednio w LAN (tinytuya), bramki i podurządzenia Zigbee, oraz import/sterowanie "
                "przez Tuya Cloud (w tym detekcja ruchu z kamer Tuya).",
    config_url="/plugins/tuya",
    config_key="tuya",
    has_poll_interval=True,
    min_poll_interval_seconds=3,
    toggle_route="toggle_prereq_connections_live",
    describe_state=describe_state,
    category="Tuya",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_cloud_accounts() -> dict:
        return get_config_store().get("config").get("tuya", {}).get("cloud_accounts", {}) or {}

    def _save_cloud_accounts(accounts: dict) -> None:
        current = get_config_store().get("config")
        current.setdefault("tuya", {})["cloud_accounts"] = accounts
        get_config_store().set("config", current)

    def _resolve_account_name(device_cfg: dict, accounts: dict) -> str | None:
        # Puste "cloud_account" (urządzenie sprzed wprowadzenia wielu kont, albo
        # zapisane przez kreator, który zna tylko jedno konto) -> pierwsze
        # skonfigurowane konto, tak samo jak w TuyaCloudController.
        return device_cfg.get("cloud_account") or (next(iter(accounts), None))

    @app.route("/plugins/tuya")
    async def tuya_plugin_page():
        return await render_template("tuya_local/index.html", regions=TUYA_CLOUD_REGIONS)

    @app.route("/plugins/tuya_cloud")
    async def tuya_cloud_plugin_page_redirect():
        # Tuya Cloud (dawniej osobna wtyczka) - teraz jedna, wspólna strona Tuya.
        return redirect("/plugins/tuya")

    @app.route("/api/plugins/tuya/devices", methods=["GET"])
    async def api_plugin_tuya_devices_get():
        tuya = get_config_raw().get("tuya", {})
        return jsonify({
            "devices": tuya_devices_to_rows(tuya.get("devices", {})),
            "zigbee_gateways": zigbee_gateways_to_rows(tuya.get("zigbee_gateways", {})),
        })

    @app.route("/api/plugins/tuya/devices", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_devices_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

        errors = validate_tuya_devices(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        tuya_devices_dict = build_tuya_devices_dict(form)
        existing = get_config_store().get("tuya_devices")
        existing["devices"] = tuya_devices_dict["devices"]
        existing["zigbee_gateways"] = tuya_devices_dict["zigbee_gateways"]
        get_config_store().set("tuya_devices", existing)

        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(tuya_devices_dict["devices"]),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/tuya/devices/test", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_devices_test():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        device_id = (form.get("id") or "").strip()
        if not device_id:
            return jsonify({"error": "Uzupełnij ID urządzenia przed testem"}), 400

        try:
            if form.get("mode") == "local_zigbee":
                status = await test_local_device_status(
                    device_id, cid=form.get("cid"),
                    gateway_ip=form.get("gateway_ip"), gateway_id=form.get("gateway_id"),
                    gateway_key=form.get("gateway_key"), gateway_version=form.get("gateway_version"),
                )
            else:
                status = await test_local_device_status(
                    device_id, ip=form.get("ip"), local_key=form.get("local_key"), version=form.get("version"),
                )
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd testu: {exc}"}), 502

        return jsonify({"message": f"Połączono - odpowiedź: {status}"})

    @app.route("/api/plugins/tuya/scan", methods=["POST"])
    async def api_plugin_tuya_scan():
        tuya = get_config_raw().get("tuya", {})
        known_by_id: dict[str, str] = {}
        known_gateway_by_id: dict[str, str] = {}
        tuyadevices_for_scan = []
        for name, cfg in tuya.get("cloud_devices", {}).items():
            if cfg.get("id"):
                known_by_id[cfg["id"]] = name
        for name, cfg in tuya.get("devices", {}).items():
            if cfg.get("id"):
                known_by_id[cfg["id"]] = name
            if cfg.get("id") and cfg.get("local_key"):
                tuyadevices_for_scan.append({"id": cfg["id"], "key": cfg["local_key"], "name": name})
        for name, cfg in tuya.get("zigbee_gateways", {}).items():
            if cfg.get("id"):
                known_gateway_by_id[cfg["id"]] = name
            if cfg.get("id") and cfg.get("local_key"):
                tuyadevices_for_scan.append({"id": cfg["id"], "key": cfg["local_key"], "name": name})
        scan_networks = tuya.get("scan_networks") or []

        try:
            devices = await asyncio.get_event_loop().run_in_executor(
                None, lambda: scan_tuya_network(networks=scan_networks, tuyadevices=tuyadevices_for_scan),
            )
        except Exception as exc:
            return jsonify({"error": f"Błąd skanowania: {exc}"}), 502
        for d in devices:
            d["known_name"] = known_by_id.get(d.get("id"), "")
            # Bramka Zigbee zapisana już wcześniej (np. bez adresu IP, dopóki nie
            # odpowiadała w sieci) - osobne pole niż known_name, żeby panel mógł
            # od razu zaproponować uzupełnienie jej adresu IP zamiast dodawać
            # duplikat jako zwykłe urządzenie.
            d["known_gateway_name"] = known_gateway_by_id.get(d.get("id"), "")
        return jsonify({"devices": devices})

    @app.route("/api/plugins/tuya/scan-networks", methods=["GET"])
    async def api_plugin_tuya_scan_networks_get():
        tuya = get_config_raw().get("tuya", {})
        return jsonify({"scan_networks": tuya.get("scan_networks") or []})

    @app.route("/api/plugins/tuya/scan-networks", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_scan_networks_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict) or not isinstance(form.get("scan_networks"), list):
            return jsonify({"error": "Nieprawidłowe dane - oczekiwano listy 'scan_networks'"}), 400

        networks = []
        wide_ranges = []
        for entry in form["scan_networks"]:
            value = (entry or "").strip() if isinstance(entry, str) else ""
            if not value:
                continue
            try:
                net = ipaddress.ip_network(value, strict=False)
            except ValueError:
                return jsonify({"error": f"Nieprawidłowy adres IP albo zakres CIDR: '{value}'"}), 400
            if net.num_addresses > 64:
                wide_ranges.append(f"{value} ({net.num_addresses} adresów)")
            networks.append(value)

        current = get_config_store().get("config")
        current.setdefault("tuya", {})["scan_networks"] = networks
        get_config_store().set("config", current)
        reload_bridge_config()
        enqueue_bridge_reload()

        message = "Zapisano. Zmiana zadziała przy następnym skanowaniu, bez restartu procesu."
        if wide_ranges:
            message += (
                " ⚠️ Bardzo szeroki zakres: " + ", ".join(wide_ranges) + " - aktywny skan tylu adresów "
                "naraz jest wolny i może chwilowo zakłócić komunikację z już połączonymi urządzeniami "
                "Tuya (błędy typu \"Check device key or version\" na innych urządzeniach w trakcie "
                "skanowania) - rozważ węższy zakres albo pojedyncze adresy zamiast całej podsieci."
            )

        return jsonify({
            "scan_networks": networks,
            "message": message,
        })

    @app.route("/api/plugins/tuya_cloud/accounts", methods=["GET"])
    async def api_plugin_tuya_cloud_accounts_get():
        return jsonify({"accounts": cloud_accounts_to_rows(_get_cloud_accounts())})

    @app.route("/api/plugins/tuya_cloud/accounts", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_cloud_accounts_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict) or not isinstance(form.get("accounts"), list):
            return jsonify({"error": "Nieprawidłowe dane - oczekiwano listy 'accounts'"}), 400

        errors = validate_cloud_accounts(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        accounts = build_cloud_accounts_dict(form, existing=_get_cloud_accounts())
        _save_cloud_accounts(accounts)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(accounts),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/tuya_cloud/devices", methods=["GET"])
    async def api_plugin_tuya_cloud_devices_get():
        cloud_devices = get_config_raw().get("tuya", {}).get("cloud_devices", {})
        return jsonify({"devices": cloud_devices_to_rows(cloud_devices)})

    @app.route("/api/plugins/tuya_cloud/devices", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_cloud_devices_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

        errors = validate_cloud_devices(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        tuya_devices_dict = build_tuya_devices_dict(form)
        existing = get_config_store().get("tuya_devices")
        existing["cloud_devices"] = tuya_devices_dict["cloud_devices"]
        get_config_store().set("tuya_devices", existing)

        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(tuya_devices_dict["cloud_devices"]),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/tuya_cloud/cloud-import", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_cloud_cloud_import():
        accounts = _get_cloud_accounts()
        if not accounts:
            return jsonify({
                "error": "Najpierw dodaj konto Tuya Cloud wyżej (nazwa, region, Access ID, Access Secret).",
            }), 400

        form = await request.get_json(silent=True) or {}
        account_name = (form.get("account") or "").strip() or next(iter(accounts))
        account = accounts.get(account_name)
        if account is None:
            return jsonify({"error": f"Nieznane konto Tuya Cloud: '{account_name}'"}), 400
        api_region = account.get("api_region", "")
        access_id = account.get("access_id", "")
        access_key = account.get("access_key", "")
        if not api_region or not access_id or not access_key:
            return jsonify({
                "error": f"Konto '{account_name}': uzupełnij region, Access ID i Access Secret wyżej.",
            }), 400

        scan_networks = get_config_raw().get("tuya", {}).get("scan_networks") or []
        try:
            devices = await asyncio.get_event_loop().run_in_executor(
                None, fetch_tuya_cloud_devices, api_region, access_id, access_key, scan_networks,
            )
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd importu z Tuya Cloud: {exc}"}), 502
        for d in devices:
            d["cloud_account"] = account_name
        return jsonify({"devices": devices, "account": account_name})

    async def _refresh_dp_schema(devices: dict, api_region: str, access_id: str, access_key: str):
        """Pobiera z Tuya Cloud opisy pól (dp_schema) dla przekazanego słownika
        urządzeń (kluczowanego nazwą) i nadpisuje w miejscu pole "dp_schema" -
        wspólne dla urządzeń lokalnych/Zigbee (tuya.devices) i chmurowych
        (tuya.cloud_devices), bo obie sekcje przechowują dp_schema identycznie."""
        device_ids = {name: cfg["id"] for name, cfg in devices.items() if cfg.get("id")}
        if not device_ids:
            return [], []
        schemas, _fetch_errors = await asyncio.get_event_loop().run_in_executor(
            None, fetch_dp_schemas_for_devices, api_region, access_id, access_key, list(device_ids.values()),
        )
        updated, failed = [], []
        for name, dev_id in device_ids.items():
            if dev_id in schemas and schemas[dev_id]:
                devices[name]["dp_schema"] = schemas[dev_id]
                updated.append(name)
            else:
                failed.append(name)
        return updated, failed

    def _group_by_account(devices: dict, accounts: dict) -> dict[str, dict]:
        """Urządzenia lokalne (WiFi/Zigbee) nie mają "cloud_account" - trafiają
        wszystkie pod konto domyślne, bo dp_schema dla nich pochodzi zawsze z
        tego samego konta, na którym zostały pierwotnie zaimportowane/dodane."""
        grouped: dict[str, dict] = {}
        for name, cfg in devices.items():
            account_name = _resolve_account_name(cfg, accounts)
            if account_name:
                grouped.setdefault(account_name, {})[name] = cfg
        return grouped

    @app.route("/api/plugins/tuya/devices/refresh-dp-schema", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_refresh_dp_schema():
        accounts = _get_cloud_accounts()
        if not accounts:
            return jsonify({
                "error": "Najpierw dodaj konto Tuya Cloud wyżej (nazwa, region, Access ID, Access Secret).",
            }), 400

        tuya = get_config_raw().get("tuya", {})
        local_devices = {name: dict(cfg) for name, cfg in tuya.get("devices", {}).items()}
        cloud_devices = {name: dict(cfg) for name, cfg in tuya.get("cloud_devices", {}).items()}
        if not local_devices and not cloud_devices:
            return jsonify({"error": "Brak skonfigurowanych urządzeń z ID do opisania"}), 400

        updated: list[str] = []
        failed: list[str] = []
        try:
            for account_name, devices_for_account in _group_by_account(local_devices, accounts).items():
                account = accounts[account_name]
                if not (account.get("access_id") and account.get("access_key")):
                    failed.extend(devices_for_account)
                    continue
                u, f = await _refresh_dp_schema(
                    devices_for_account, account.get("api_region", "eu"), account["access_id"], account["access_key"],
                )
                updated.extend(u)
                failed.extend(f)
            for account_name, devices_for_account in _group_by_account(cloud_devices, accounts).items():
                account = accounts[account_name]
                if not (account.get("access_id") and account.get("access_key")):
                    failed.extend(devices_for_account)
                    continue
                u, f = await _refresh_dp_schema(
                    devices_for_account, account.get("api_region", "eu"), account["access_id"], account["access_key"],
                )
                updated.extend(u)
                failed.extend(f)
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd komunikacji z Tuya Cloud: {exc}"}), 502

        tuya_devices = get_config_store().get("tuya_devices")
        tuya_devices["devices"] = local_devices
        tuya_devices["cloud_devices"] = cloud_devices
        get_config_store().set("tuya_devices", tuya_devices)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "updated": updated, "failed": failed,
            "message": f"Zaktualizowano opisy pól dla {len(updated)}/{len(updated) + len(failed)} urządzeń.",
        })

    @app.route("/api/plugins/tuya_cloud/devices/rescan", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_cloud_rescan():
        accounts = _get_cloud_accounts()
        if not accounts:
            return jsonify({
                "error": "Najpierw dodaj konto Tuya Cloud wyżej (nazwa, region, Access ID, Access Secret).",
            }), 400

        cloud_devices = get_config_raw().get("tuya", {}).get("cloud_devices", {})
        by_id = {cfg["id"]: name for name, cfg in cloud_devices.items() if cfg.get("id")}
        # Urządzenia JUŻ w trybie "Lokalnie (Zigbee)" - ich "cid" (node_id w sieci
        # Zigbee danej bramki) wpisuje się raz, ręcznie albo przy imporcie, i nic
        # później nie sprawdza, czy wciąż się zgadza z tym, co faktycznie zna Tuya
        # Cloud - stąd druga, niezależna funkcja tego samego przycisku: weryfikacja
        # (nigdy automatyczna korekta bez kliknięcia) już skonfigurowanych urządzeń.
        zigbee_devices = {
            name: cfg for name, cfg in get_config_raw().get("tuya", {}).get("devices", {}).items()
            if cfg.get("gateway") and cfg.get("id")
        }
        zigbee_by_id = {cfg["id"]: name for name, cfg in zigbee_devices.items()}
        if not by_id and not zigbee_by_id:
            return jsonify({
                "error": "Brak skonfigurowanych urządzeń chmurowych ani urządzeń Zigbee do przeskanowania",
            }), 400
        scan_networks = get_config_raw().get("tuya", {}).get("scan_networks") or []
        # Bramki Zigbee już dodane ręcznie/z importu - potrzebne, żeby rozpoznać,
        # które chmurowe urządzenie jest podurządzeniem Zigbee wskazującym na JUŻ
        # skonfigurowaną bramkę (a nie tylko "widziane" przez Tuya Cloud).
        zigbee_gateways = get_config_raw().get("tuya", {}).get("zigbee_gateways", {})
        gateway_id_to_name = {cfg["id"]: name for name, cfg in zigbee_gateways.items() if cfg.get("id")}

        # Każde konto ma swoją, niezależną listę urządzeń w Tuya Cloud - trzeba
        # odpytać każde skonfigurowane konto osobno i dopasować wynik tylko do
        # urządzeń, które faktycznie do niego należą (żeby nie mylić urządzeń
        # o takim samym ID z różnych kont, choć w praktyce ID są globalnie unikalne).
        results = []
        cid_checks = []
        cloud_devices_by_account = _group_by_account(cloud_devices, accounts)
        zigbee_devices_by_account = _group_by_account(zigbee_devices, accounts)
        queried_any_account = False
        for account_name, account in accounts.items():
            if not (account.get("access_id") and account.get("access_key")):
                continue
            by_id_for_account = {
                cfg["id"]: name for name, cfg in cloud_devices_by_account.get(account_name, {}).items()
            }
            zigbee_by_id_for_account = {
                cfg["id"]: name for name, cfg in zigbee_devices_by_account.get(account_name, {}).items()
            }
            if not by_id_for_account and not zigbee_by_id_for_account:
                continue
            queried_any_account = True
            try:
                devices = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_tuya_cloud_devices,
                    account.get("api_region", "eu"), account["access_id"], account["access_key"], scan_networks,
                )
            except ConnectionError as exc:
                return jsonify({"error": f"Konto '{account_name}': {exc}"}), 502
            except Exception as exc:
                return jsonify({"error": f"Konto '{account_name}': błąd komunikacji z Tuya Cloud: {exc}"}), 502

            for d in devices:
                dev_id = d.get("id", "")
                cloud_cid = d.get("node_id") or dev_id

                name = by_id_for_account.get(dev_id)
                if name:
                    gateway_name = gateway_id_to_name.get(d.get("gateway_id") or "", "")
                    results.append({
                        "name": name,
                        "id": dev_id,
                        "ip": d.get("ip", ""),
                        "local_key": d.get("local_key", ""),
                        "version": d.get("version", ""),
                        "dp_schema": d.get("dp_schema", {}),
                        "can_move_to_local": bool(d.get("ip") and d.get("local_key")),
                        "can_move_to_gateway": bool(gateway_name),
                        "gateway_name": gateway_name,
                        "cid": cloud_cid,
                        "cloud_account": account_name,
                    })

                zigbee_name = zigbee_by_id_for_account.get(dev_id)
                if zigbee_name:
                    current_cid = (zigbee_devices[zigbee_name].get("cid") or dev_id).strip()
                    cid_checks.append({
                        "name": zigbee_name,
                        "id": dev_id,
                        "current_cid": current_cid,
                        "cloud_cid": cloud_cid,
                        "mismatch": current_cid != cloud_cid,
                        "cloud_account": account_name,
                    })

        if not queried_any_account:
            return jsonify({
                "error": "Żadne skonfigurowane konto Tuya Cloud nie ma kompletnych poświadczeń "
                         "(Access ID + Access Secret) - uzupełnij je wyżej.",
            }), 400

        movable_local = sum(1 for r in results if r["can_move_to_local"])
        movable_gateway = sum(1 for r in results if r["can_move_to_gateway"])
        mismatched_cid = sum(1 for c in cid_checks if c["mismatch"])
        message_parts = []
        if by_id:
            message_parts.append(
                f"Przeskanowano {len(results)}/{len(by_id)} urządzeń chmurowych - "
                f"{movable_local} może przejść na sterowanie lokalne (WiFi), "
                f"{movable_gateway} na bramkę Zigbee."
            )
        if zigbee_by_id:
            message_parts.append(
                f"Sprawdzono cid {len(cid_checks)}/{len(zigbee_by_id)} urządzeń Zigbee - "
                f"{mismatched_cid} ma nieaktualny cid względem Tuya Cloud."
            )
        return jsonify({
            "devices": results,
            "zigbee_cid_checks": cid_checks,
            "message": " ".join(message_parts) if message_parts else "Nic nie odnaleziono w Tuya Cloud.",
        })

    def _get_tuya_section() -> dict:
        return get_config_store().get("config").get("tuya", {})

    def _save_tuya_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["tuya"] = section
        get_config_store().set("config", current)

    register_settings_route(
        app, "tuya_local", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_tuya_section, save_section=_save_tuya_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
