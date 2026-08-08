"""Panel webowy - podgląd historii i zdarzeń z bridge'a.

Celowo lekki: Flask + wbudowany serwer WSGI (waitress) - żadna z tych
bibliotek nie ma problemu z architekturą ARMv6 RPi Zero W, w przeciwieństwie
do Grafany/InfluxDB (Go, brak binarek dla armv6). Cały ciężki render
wykresów robi przeglądarka (Chart.js) - RPi tylko serwuje JSON z SQLite.

Uruchomienie (osobny proces od głównego bridge'a - czyta ten sam plik
SQLite, więc nie trzeba go integrować z pętlą asyncio):
    python -m dashboard.app --config config.yaml
"""
from __future__ import annotations

import argparse
import time

import yaml
from flask import Flask, jsonify, render_template, request

from bridge.config import BridgeConfig
from bridge.history_store import HistoryStore
from dashboard.auth import AuthGuard, require_auth

app = Flask(__name__)
history: HistoryStore  # ustawiane w create_app()
sensors_file_path = None  # ustawiane w create_app() - ścieżka do sensors.yaml
auth_guard: AuthGuard  # ustawiane w create_app()
satel_partitions_cfg: dict = {}  # nazwa -> numer strefy, ustawiane w create_app()


def create_app(config: BridgeConfig) -> Flask:
    global history, sensors_file_path, auth_guard, satel_partitions_cfg
    db_path = config.raw.get("dashboard", {}).get("db_path", "bridge_history.db")
    history = HistoryStore(db_path)
    sensors_file_path = config.sensors_file
    auth_guard = AuthGuard(config.raw.get("dashboard", {}).get("auth"))
    satel_partitions_cfg = config.satel.get("partitions", {"Dom": config.satel.get("partition", 1)})
    return app


def _parse_range() -> tuple[float, float]:
    now = time.time()
    hours = float(request.args.get("hours", 24))
    ts_from = float(request.args.get("from", now - hours * 3600))
    ts_to = float(request.args.get("to", now))
    return ts_from, ts_to


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config_page():
    return render_template("config.html")


@app.route("/api/config/sensors", methods=["GET"])
def api_config_sensors_get():
    if not sensors_file_path.exists():
        return jsonify([])
    with open(sensors_file_path, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f) or {}
    return jsonify(content.get("sensors", []))


@app.route("/api/config/sensors", methods=["POST"])
@require_auth()
def api_config_sensors_post():
    """Nadpisuje CAŁĄ zawartość sensors.yaml przesłaną listą. Walidacja jest
    celowo minimalna (tylko to, co faktycznie zepsułoby resztę bridge'a przy
    starcie) - reszta pól ma sensowne wartości domyślne."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, list):
        return jsonify({"error": "Oczekiwano listy czujników w ciele żądania"}), 400

    cleaned = []
    seen_names = set()
    for i, item in enumerate(payload):
        name = (item.get("name") or "").strip()
        mqtt_topic = (item.get("mqtt_topic") or "").strip()
        if not name or not mqtt_topic:
            return jsonify({"error": f"Wiersz {i + 1}: pola 'name' i 'mqtt_topic' są wymagane"}), 400
        if name in seen_names:
            return jsonify({"error": f"Zduplikowana nazwa czujnika: '{name}'"}), 400
        seen_names.add(name)

        targets = item.get("targets") or {}
        cleaned.append({
            "name": name,
            "label": (item.get("label") or name).strip(),
            "mqtt_topic": mqtt_topic,
            "targets": {
                "history": bool(targets.get("history", True)),
                "mqtt_publish": bool(targets.get("mqtt_publish", False)),
                "tuya_virtual_device": bool(targets.get("tuya_virtual_device", False)),
                "tuya_dp": (targets.get("tuya_dp") or "").strip(),
            },
        })

    with open(sensors_file_path, "w", encoding="utf-8") as f:
        f.write("# Wygenerowane z panelu webowego (/config) - patrz sensors.example.yaml po opis pól\n")
        yaml.safe_dump({"sensors": cleaned}, f, allow_unicode=True, sort_keys=False)

    return jsonify({
        "saved": len(cleaned),
        "message": "Zapisano. Zmiany zaczną działać po restarcie procesu bridge'a "
                    "(nie samego panelu) - np. `sudo systemctl restart satel-tuya-bridge`.",
    })


@app.route("/api/sensors")
def api_sensors():
    return jsonify(history.get_sensors())


@app.route("/api/history")
def api_history():
    sensor = request.args.get("sensor")
    if not sensor:
        return jsonify({"error": "brak parametru 'sensor'"}), 400
    ts_from, ts_to = _parse_range()
    return jsonify(history.get_history(sensor, ts_from, ts_to))


@app.route("/api/events")
def api_events():
    event_type = request.args.get("type") or None
    ts_from, ts_to = _parse_range()
    limit = int(request.args.get("limit", 500))
    return jsonify(history.get_events(event_type, ts_from, ts_to, limit))


@app.route("/api/alerts/active")
def api_alerts_active():
    return jsonify(history.get_active_alerts())


@app.route("/api/alerts/history")
def api_alerts_history():
    ts_from, ts_to = _parse_range()
    limit = int(request.args.get("limit", 200))
    return jsonify(history.get_alerts_history(ts_from, ts_to, limit))


@app.route("/api/status")
def api_status():
    category = request.args.get("category") or None
    return jsonify(history.get_status(category))


@app.route("/api/satel/partitions")
def api_satel_partitions():
    """Lista nazwanych stref do wyboru w panelu - samo nazewnictwo nie jest
    wrażliwe, więc ten endpoint zostaje publiczny (bez autoryzacji).
    Faktyczne uzbrojenie/rozbrojenie wymaga hasła - patrz endpointy niżej."""
    return jsonify(satel_partitions_cfg)


@app.route("/api/satel/arm", methods=["POST"])
@require_auth()
def api_satel_arm():
    payload = request.get_json(silent=True) or {}
    partitions = payload.get("partitions") or list(satel_partitions_cfg.values())
    mode = payload.get("mode", 0)
    command_id = history.enqueue_command("satel_arm", {"partitions": partitions, "mode": mode})
    history.write_event("system", source="dashboard", detail={"message": f"Zlecono uzbrojenie stref {partitions}"})
    return jsonify({"command_id": command_id, "status": "pending"})


@app.route("/api/satel/disarm", methods=["POST"])
@require_auth()
def api_satel_disarm():
    payload = request.get_json(silent=True) or {}
    partitions = payload.get("partitions") or list(satel_partitions_cfg.values())
    command_id = history.enqueue_command("satel_disarm", {"partitions": partitions})
    history.write_event("system", source="dashboard", detail={"message": f"Zlecono rozbrojenie stref {partitions}"})
    return jsonify({"command_id": command_id, "status": "pending"})


@app.route("/api/commands/<int:command_id>")
def api_command_status(command_id: int):
    """Odczyt statusu wcześniej zleconej komendy (do polowania z frontendu,
    żeby pokazać czy uzbrojenie faktycznie się powiodło) - samo zlecenie
    komendy wymagało hasła, sam odczyt statusu jest nieszkodliwy (nie
    pozwala niczego zmienić), więc zostaje publiczny."""
    cmd = history.get_command(command_id)
    if cmd is None:
        return jsonify({"error": "Nie znaleziono komendy"}), 404
    return jsonify(cmd)


@app.route("/healthz")
def healthz():
    """Prosty endpoint zdrowia do zewnętrznego monitoringu (np. uptime-kuma,
    healthcheck w systemd/monit). Zwraca 200 jeśli wszystkie monitorowane
    komponenty są połączone, 503 jeśli którykolwiek zgłasza rozłączenie."""
    health_statuses = history.get_status("system_health")
    disconnected = [s for s in health_statuses if not s["value"].startswith("połączono")]
    body = {
        "ok": len(disconnected) == 0,
        "components": {s["key"].replace("health:", ""): s["value"] for s in health_statuses},
    }
    return jsonify(body), 200 if not disconnected else 503


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    config = BridgeConfig.load(args.config)
    create_app(config)

    from waitress import serve
    print(f"Panel dostępny pod http://{args.host}:{args.port}")
    serve(app, host=args.host, port=args.port, threads=2)


if __name__ == "__main__":
    main()
