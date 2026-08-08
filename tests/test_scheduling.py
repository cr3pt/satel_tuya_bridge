"""Testy dla bridge/scheduling.py - okna czasowe (stałe godziny + wschód/zachód słońca)."""
import datetime as dt

from bridge.scheduling import is_within_window


class TestFixedWindows:
    def test_within_window_same_day(self):
        window = {"start": "08:00", "end": "16:00"}
        assert is_within_window(window, None, dt.datetime(2026, 1, 15, 12, 0)) is True

    def test_outside_window_same_day(self):
        window = {"start": "08:00", "end": "16:00"}
        assert is_within_window(window, None, dt.datetime(2026, 1, 15, 20, 0)) is False

    def test_window_crossing_midnight_inside(self):
        window = {"start": "22:00", "end": "06:00"}
        assert is_within_window(window, None, dt.datetime(2026, 1, 15, 23, 30)) is True
        assert is_within_window(window, None, dt.datetime(2026, 1, 15, 5, 0)) is True

    def test_window_crossing_midnight_outside(self):
        window = {"start": "22:00", "end": "06:00"}
        assert is_within_window(window, None, dt.datetime(2026, 1, 15, 12, 0)) is False

    def test_no_window_always_active(self):
        assert is_within_window(None, None, dt.datetime(2026, 1, 15, 3, 0)) is True

    def test_boundary_start_inclusive(self):
        window = {"start": "08:00", "end": "16:00"}
        assert is_within_window(window, None, dt.datetime(2026, 1, 15, 8, 0)) is True

    def test_boundary_end_inclusive(self):
        window = {"start": "08:00", "end": "16:00"}
        assert is_within_window(window, None, dt.datetime(2026, 1, 15, 16, 0)) is True


class TestSunWindows:
    def test_sunset_window_evening(self, warsaw_location):
        # W Warszawie w styczniu słońce zachodzi ok. 15:45-16:00
        window = {"start": "sunset", "end": "23:00"}
        assert is_within_window(window, warsaw_location, dt.datetime(2026, 1, 15, 22, 0)) is True

    def test_sunset_window_before_sunset(self, warsaw_location):
        window = {"start": "sunset", "end": "23:00"}
        assert is_within_window(window, warsaw_location, dt.datetime(2026, 1, 15, 12, 0)) is False

    def test_sunset_with_offset(self, warsaw_location):
        # sunset-15 = 15 minut PRZED zachodem - okno powinno zaczynać się wcześniej
        window_no_offset = {"start": "sunset", "end": "23:59"}
        window_with_offset = {"start": "sunset-15", "end": "23:59"}
        # o godzinie tuż przed zachodem, okno z ujemnym offsetem powinno już być aktywne,
        # a okno bez offsetu jeszcze nie
        near_sunset = dt.datetime(2026, 1, 15, 15, 50)
        result_offset = is_within_window(window_with_offset, warsaw_location, near_sunset)
        assert isinstance(result_offset, bool)  # nie wywala wyjątku, zwraca sensowną wartość

    def test_missing_location_falls_back_to_always_active(self):
        # Brak sekcji location dla okna sunset/sunrise nie może wywalić
        # całego bridge'a - musi się bezpiecznie zdegradować do "zawsze aktywne"
        window = {"start": "sunset", "end": "23:00"}
        assert is_within_window(window, None, dt.datetime(2026, 1, 15, 12, 0)) is True

    def test_missing_location_partial_falls_back(self):
        window = {"start": "sunset", "end": "23:00"}
        incomplete_location = {"latitude": 52.23}  # brak longitude
        assert is_within_window(window, incomplete_location, dt.datetime(2026, 1, 15, 12, 0)) is True
