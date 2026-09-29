import tempfile
import unittest
from pathlib import Path

from app import SmartHomeController


class SmartHomeTests(unittest.TestCase):
    def setUp(self):
        # Каждый тест использует собственную временную базу данных.
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "home.db"
        self.home = SmartHomeController(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_motion_and_temperature_rules(self):
        # Проверяем свет от движения и два температурных порога.
        state = self.home.command("/api/sensor", {"id": "motion", "value": True})
        self.assertTrue(state["devices"]["hall_light"]["on"])
        state = self.home.command("/api/sensor", {"id": "temperature", "value": 19})
        self.assertTrue(state["devices"]["heater"]["on"])
        state = self.home.command("/api/sensor", {"id": "temperature", "value": 22})
        self.assertTrue(state["devices"]["heater"]["on"])
        state = self.home.command("/api/sensor", {"id": "temperature", "value": 24})
        self.assertFalse(state["devices"]["heater"]["on"])

    def test_leak_independent_of_automation(self):
        # Сигнал протечки имеет приоритет даже при отключённых обычных правилах.
        self.home.command("/api/automation", {"enabled": False})
        state = self.home.command("/api/sensor", {"id": "leak", "value": True})
        self.assertTrue(state["devices"]["leak_alarm"]["on"])
        with self.assertRaises(ValueError):
            self.home.command("/api/device", {"id": "leak_alarm", "on": False})

    def test_scene_and_persistence(self):
        # После повторного открытия базы сохраняются сценарий и журнал.
        state = self.home.command("/api/scene", {"name": "away"})
        self.assertFalse(state["devices"]["living_light"]["on"])
        again = SmartHomeController(self.path).snapshot()
        self.assertEqual(again["scene"], "away")
        self.assertTrue(any("сценарий" in e["message"] for e in again["events"]))

    def test_invalid_temperature_is_rejected_without_event(self):
        # Неверный ввод не должен менять состояние или добавлять событие.
        with self.assertRaises(ValueError):
            self.home.command("/api/sensor", {"id": "temperature", "value": 100})
        self.assertEqual(self.home.snapshot()["events"], [])


if __name__ == "__main__":
    unittest.main()
