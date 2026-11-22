import unittest
from datetime import date, timedelta

from app.stations import normalize_station
from app.utils import format_date, monitor_key, route_url, validate_travel_date


class UtilityTests(unittest.TestCase):
    def test_station_alias(self):
        self.assertEqual(normalize_station("Минск"), "Минск-Пассажирский")

    def test_empty_station(self):
        with self.assertRaises(ValueError):
            normalize_station("   ")

    def test_date_validation(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(validate_travel_date(tomorrow).isoformat(), tomorrow)
        with self.assertRaises(ValueError):
            validate_travel_date("2026-02-31")

    def test_past_date(self):
        with self.assertRaises(ValueError):
            validate_travel_date((date.today() - timedelta(days=1)).isoformat())

    def test_route_url(self):
        url = route_url("Минск-Пассажирский", "Брест-Центральный", "2026-12-01")
        self.assertTrue(url.startswith("https://pass.rw.by/ru/route/?"))
        self.assertIn("date=2026-12-01", url)

    def test_monitor_key_contains_route(self):
        a = monitor_key("Минск", "Брест", "2026-12-01", "123")
        b = monitor_key("Гомель", "Брест", "2026-12-01", "123")
        self.assertNotEqual(a, b)

    def test_format_date(self):
        self.assertEqual(format_date("2026-12-01"), "01.12.2026")


if __name__ == "__main__":
    unittest.main()
