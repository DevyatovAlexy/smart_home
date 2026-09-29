# Локальный прототип системы управления умным домом.
# Запуск: python app.py --port 8765

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


DEVICE_NAMES = {
    "hall_light": "Свет в прихожей",
    "living_light": "Свет в гостиной",
    "heater": "Обогреватель",
    "leak_alarm": "Сигнал протечки",
}
SENSOR_NAMES = {"temperature", "motion", "leak"}
SCENES = {
    "home": {"hall_light": True, "living_light": True, "heater": False},
    "away": {"hall_light": False, "living_light": False, "heater": False},
    "night": {"hall_light": False, "living_light": False, "heater": False},
}


@dataclass
class Device:
    id: str
    name: str
    on: bool = False


class SmartHomeController:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        # Запросы меняют общее состояние по очереди.
        self.lock = threading.RLock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, message TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('state', ?)",
                (json.dumps(self._initial_state(), ensure_ascii=False),),
            )

    @staticmethod
    def _initial_state():
        return {
            "devices": {
                key: asdict(Device(key, name)) for key, name in DEVICE_NAMES.items()
            },
            "sensors": {"temperature": 21.0, "motion": False, "leak": False},
            "automation": True,
            "scene": "home",
        }

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _load(self, db):
        row = db.execute("SELECT value FROM settings WHERE key='state'").fetchone()
        return json.loads(row[0])

    def _save(self, db, state, message):
        # Состояние и событие сохраняются в одной транзакции.
        db.execute(
            "UPDATE settings SET value=? WHERE key='state'",
            (json.dumps(state, ensure_ascii=False),),
        )
        db.execute(
            "INSERT INTO events(at,message) VALUES (?,?)",
            (datetime.now().astimezone().isoformat(timespec="seconds"), message),
        )
        db.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 100)"
        )

    def snapshot(self):
        with self.lock, self._connect() as db:
            state = self._load(db)
            state["events"] = [
                {"at": at, "message": message}
                for at, message in db.execute(
                    "SELECT at,message FROM events ORDER BY id DESC LIMIT 20"
                )
            ]
            return state

    @staticmethod
    def _apply_automation(state):
        sensors, devices = state["sensors"], state["devices"]
        # Сигнал протечки действует даже при отключённой автоматизации.
        devices["leak_alarm"]["on"] = sensors["leak"]
        if not state["automation"]:
            return
        devices["hall_light"]["on"] = sensors["motion"]
        # Между порогами обогреватель сохраняет прежнее состояние.
        if sensors["temperature"] < 20:
            devices["heater"]["on"] = True
        elif sensors["temperature"] > 23:
            devices["heater"]["on"] = False

    def command(self, path, data):
        if not isinstance(data, dict):
            raise ValueError("Ожидается объект JSON")
        with self.lock, self._connect() as db:
            state = self._load(db)
            if path == "/api/device":
                key, value = data.get("id"), data.get("on")
                if (
                    key not in DEVICE_NAMES
                    or key == "leak_alarm"
                    or type(value) is not bool
                ):
                    raise ValueError("Недопустимое устройство или состояние")
                state["devices"][key]["on"] = value
                message = f"{DEVICE_NAMES[key]}: {'включено' if value else 'выключено'} вручную"
            elif path == "/api/sensor":
                key, value = data.get("id"), data.get("value")
                if key not in SENSOR_NAMES:
                    raise ValueError("Неизвестный датчик")
                if key == "temperature":
                    if type(value) not in (int, float) or not -30 <= value <= 60:
                        raise ValueError("Температура должна быть от -30 до 60")
                    value = float(value)
                elif type(value) is not bool:
                    raise ValueError("Ожидается логическое значение")
                state["sensors"][key] = value
                self._apply_automation(state)
                message = f"Датчик {key}: {value}"
            elif path == "/api/scene":
                name = data.get("name")
                if name not in SCENES:
                    raise ValueError("Неизвестный сценарий")
                for key, value in SCENES[name].items():
                    state["devices"][key]["on"] = value
                state["scene"] = name
                self._apply_automation(state)
                message = f"Применён сценарий: {name}"
            elif path == "/api/automation":
                value = data.get("enabled")
                if type(value) is not bool:
                    raise ValueError("Ожидается логическое значение")
                state["automation"] = value
                self._apply_automation(state)
                message = f"Автоматизация: {'включена' if value else 'выключена'}"
            else:
                raise LookupError("Неизвестная команда")
            self._save(db, state, message)
        return self.snapshot()


def make_handler(controller, page, stylesheet):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body, kind="application/json; charset=utf-8"):
            payload = (
                body
                if isinstance(body, bytes)
                else json.dumps(body, ensure_ascii=False).encode("utf-8")
            )
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                return self._reply(200, page, "text/html; charset=utf-8")
            if path == "/style.css":
                return self._reply(200, stylesheet, "text/css; charset=utf-8")
            if path == "/api/state":
                return self._reply(200, controller.snapshot())
            return self._reply(404, {"error": "Не найдено"})

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Некорректный размер запроса")
                data = json.loads(self.rfile.read(length))
                return self._reply(200, controller.command(path, data))
            except (ValueError, json.JSONDecodeError) as exc:
                return self._reply(400, {"error": str(exc)})
            except LookupError as exc:
                return self._reply(404, {"error": str(exc)})

        def log_message(self, *_):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(
        description="Учебная система управления умным домом"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=str(Path(__file__).with_name("smart_home.db")))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть от 1 до 65535")
    controller = SmartHomeController(args.db)
    page = Path(__file__).with_name("index.html").read_bytes()
    stylesheet = Path(__file__).with_name("style.css").read_bytes()
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(controller, page, stylesheet)
    )
    print(f"Откройте http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
