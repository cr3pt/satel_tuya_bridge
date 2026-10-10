"""Testy bridge/tariffs.py - strefy G11/G12/G12w/G13, kalendarz świąt, podział
zużycia na strefy, koszt (netto/brutto/opłaty stałe) i walidacja konfiguracji."""
from datetime import date, datetime

import pytest

from bridge import tariffs
from bridge.tariffs import TZ


def ts(y, m, d, h=0, mi=0) -> float:
    return datetime(y, m, d, h, mi, tzinfo=TZ).timestamp()


def at(tid, y, m, d, h, mi=0, variant=None):
    return tariffs.resolve_tariff(tid, variant).zone_at(datetime(y, m, d, h, mi))


class TestCalendar:
    def test_easter_known_dates(self):
        assert tariffs.easter_sunday(2024) == date(2024, 3, 31)
        assert tariffs.easter_sunday(2025) == date(2025, 4, 20)
        assert tariffs.easter_sunday(2026) == date(2026, 4, 5)

    def test_movable_holidays_2026(self):
        h = tariffs.polish_holidays(2026)
        assert date(2026, 4, 6) in h      # Poniedziałek Wielkanocny
        assert date(2026, 6, 4) in h      # Boże Ciało
        assert date(2026, 5, 24) in h     # Zielone Świątki

    def test_fixed_holidays(self):
        h = tariffs.polish_holidays(2026)
        for d in [(1, 1), (1, 6), (5, 1), (5, 3), (8, 15), (11, 1), (11, 11), (12, 25), (12, 26)]:
            assert date(2026, *d) in h

    def test_wigilia_is_holiday_only_since_2025(self):
        assert date(2025, 12, 24) in tariffs.polish_holidays(2025)
        assert date(2024, 12, 24) not in tariffs.polish_holidays(2024)

    def test_free_day(self):
        assert tariffs.is_free_day(date(2026, 7, 11))        # sobota
        assert tariffs.is_free_day(date(2026, 11, 11))       # środa, święto
        assert not tariffs.is_free_day(date(2026, 7, 8))     # zwykła środa


class TestZones:
    def test_g11_single_zone(self):
        assert at("G11", 2026, 7, 8, 3) == "całodobowa"

    @pytest.mark.parametrize("h,mi,zone", [
        (5, 59, "nocna"), (6, 0, "dzienna"), (10, 0, "dzienna"), (12, 59, "dzienna"), (13, 0, "nocna"),
        (14, 45, "nocna"), (15, 0, "dzienna"), (21, 59, "dzienna"), (22, 0, "nocna"), (23, 30, "nocna"),
    ])
    def test_g12_weekday(self, h, mi, zone):
        assert at("G12", 2026, 7, 8, h, mi) == zone

    def test_g12_is_not_cheap_on_weekend(self):
        assert at("G12", 2026, 7, 11, 11) == "dzienna"

    def test_g12w_weekend_all_day_cheap(self):
        assert at("G12w", 2026, 7, 11, 11) == "nocna"
        assert at("G12w", 2026, 7, 12, 18) == "nocna"

    def test_g12w_holiday_on_weekday_is_cheap(self):
        assert at("G12w", 2026, 11, 11, 11) == "nocna"

    def test_g12w_regular_weekday_like_g12(self):
        assert at("G12w", 2026, 7, 8, 10) == "dzienna"
        assert at("G12w", 2026, 7, 8, 13, 30) == "nocna"

    def test_pge_seasonal_variant(self):
        assert at("G12", 2026, 7, 8, 16, variant="pge_sezonowa") == "nocna"     # lato 15-17
        assert at("G12", 2026, 7, 8, 13, 30, variant="pge_sezonowa") == "dzienna"
        assert at("G12", 2026, 1, 14, 16, variant="pge_sezonowa") == "dzienna"  # zima
        assert at("G12", 2026, 1, 14, 13, 30, variant="pge_sezonowa") == "nocna"

    def test_enea_variant(self):
        assert at("G12w", 2026, 7, 8, 21, 30, variant="enea") == "nocna"
        assert at("G12w", 2026, 7, 8, 13, 30, variant="enea") == "dzienna"

    def test_unknown_variant_and_tariff(self):
        with pytest.raises(ValueError):
            tariffs.resolve_tariff("G12", "nie_ma")
        with pytest.raises(ValueError):
            tariffs.resolve_tariff("G99")

    @pytest.mark.parametrize("m,d,h,mi,zone", [
        (7, 8, 8, 0, "szczyt_przedpołudniowy"), (7, 8, 12, 59, "szczyt_przedpołudniowy"),
        (7, 8, 13, 0, "pozostałe"), (7, 8, 17, 0, "pozostałe"), (7, 8, 19, 0, "szczyt_popołudniowy"),
        (7, 8, 21, 59, "szczyt_popołudniowy"), (7, 8, 22, 0, "pozostałe"),
        (1, 14, 16, 0, "szczyt_popołudniowy"), (1, 14, 20, 59, "szczyt_popołudniowy"),
        (1, 14, 21, 0, "pozostałe"), (1, 14, 18, 0, "szczyt_popołudniowy"), (1, 14, 7, 0, "szczyt_przedpołudniowy"),
    ])
    def test_g13_weekday(self, m, d, h, mi, zone):
        assert at("G13", 2026, m, d, h, mi) == zone

    def test_g13_weekend_and_holiday_have_no_peaks(self):
        assert at("G13", 2026, 7, 11, 8) == "pozostałe"
        assert at("G13", 2026, 7, 11, 20) == "pozostałe"
        assert at("G13", 2026, 11, 11, 8) == "pozostałe"


class TestHoursParsing:
    def test_wraps_midnight(self):
        assert tariffs.parse_hours("22:00-06:00") == ((1320, 1440), (0, 360))

    def test_full_day(self):
        assert tariffs.parse_hours("00:00-24:00") == ((0, 1440),)

    @pytest.mark.parametrize("bad", ["abc", "25:00-26:00", "22:10-06:00", "10:00-10:00", "10:60-11:00"])
    def test_rejects_bad(self, bad):
        with pytest.raises(ValueError):
            tariffs.parse_hours(bad)


class TestCustomTariff:
    SPEC = {"zones": ["tania", "droga"], "default_zone": "droga",
            "rules": [{"zone": "tania", "hours": ["21:00-06:00"]}, {"zone": "tania", "days": "free"}]}

    def test_custom_works_and_overrides_builtin_name(self):
        t = tariffs.resolve_tariff("G12", custom={"G12": self.SPEC})
        assert t.zones == ("tania", "droga")
        assert t.zone_at(datetime(2026, 7, 8, 22)) == "tania"
        assert t.zone_at(datetime(2026, 7, 8, 12)) == "droga"
        assert t.zone_at(datetime(2026, 7, 11, 12)) == "tania"  # days: free, domyślne hours = cała doba

    @pytest.mark.parametrize("spec", [
        {"zones": []},
        {"zones": ["a", "a"]},
        {"zones": ["a"], "default_zone": "b"},
        {"zones": ["a"], "rules": [{"zone": "x"}]},
        {"zones": ["a"], "rules": [{"zone": "a", "days": "sometimes"}]},
        {"zones": ["a"], "rules": [{"zone": "a", "months": [13]}]},
        {"zones": ["a"], "rules": [{"zone": "a", "hours": ["bad"]}]},
    ])
    def test_invalid_custom(self, spec):
        with pytest.raises(ValueError):
            tariffs.tariff_from_custom("X", spec)


class TestConsumption:
    G12 = tariffs.resolve_tariff("G12")

    def test_split_across_zone_boundary(self):
        # 1 kWh między 21:00 a 23:00 -> po 0,5 kWh w strefie dziennej i nocnej
        r = [{"t": ts(2026, 7, 8, 21), "v": 100.0}, {"t": ts(2026, 7, 8, 23), "v": 101.0}]
        c = tariffs.consumption_by_zone(r, self.G12, ts(2026, 7, 8, 21), ts(2026, 7, 8, 23))
        assert c.kwh_by_zone["dzienna"] == pytest.approx(0.5)
        assert c.kwh_by_zone["nocna"] == pytest.approx(0.5)

    def test_total_is_conserved(self):
        r = [{"t": ts(2026, 7, 8, 0), "v": 0.0}, {"t": ts(2026, 7, 8, 5), "v": 3.0},
             {"t": ts(2026, 7, 8, 9), "v": 5.5}, {"t": ts(2026, 7, 8, 14), "v": 9.0}]
        c = tariffs.consumption_by_zone(r, self.G12, ts(2026, 7, 8), ts(2026, 7, 9))
        assert c.total_kwh == pytest.approx(9.0)

    def test_period_clips_segment_proportionally(self):
        r = [{"t": ts(2026, 7, 8, 0), "v": 0.0}, {"t": ts(2026, 7, 8, 4), "v": 4.0}]
        c = tariffs.consumption_by_zone(r, self.G12, ts(2026, 7, 8, 1), ts(2026, 7, 8, 3))
        assert c.total_kwh == pytest.approx(2.0)

    def test_scale_converts_units(self):
        r = [{"t": ts(2026, 7, 8, 10), "v": 0.0}, {"t": ts(2026, 7, 8, 11), "v": 2500.0}]  # Wh
        c = tariffs.consumption_by_zone(r, self.G12, ts(2026, 7, 8, 10), ts(2026, 7, 8, 11), scale=0.001)
        assert c.total_kwh == pytest.approx(2.5)

    def test_gap_goes_to_unallocated_not_zones(self):
        r = [{"t": ts(2026, 7, 8, 0), "v": 0.0}, {"t": ts(2026, 7, 8, 20), "v": 10.0}]
        c = tariffs.consumption_by_zone(r, self.G12, ts(2026, 7, 8), ts(2026, 7, 9), max_gap_seconds=6 * 3600)
        assert c.total_kwh == 0 and c.unallocated_kwh == pytest.approx(10.0) and c.gaps == 1

    def test_counter_reset_is_skipped_and_counted(self):
        r = [{"t": ts(2026, 7, 8, 10), "v": 500.0}, {"t": ts(2026, 7, 8, 11), "v": 3.0},
             {"t": ts(2026, 7, 8, 12), "v": 4.0}]
        c = tariffs.consumption_by_zone(r, self.G12, ts(2026, 7, 8, 10), ts(2026, 7, 8, 12))
        assert c.resets == 1 and c.total_kwh == pytest.approx(1.0)

    def test_dst_uses_local_time(self):
        # 29.03.2026 zmiana na czas letni: 22:00-23:00 lokalnie to nadal strefa nocna
        r = [{"t": ts(2026, 3, 29, 22), "v": 0.0}, {"t": ts(2026, 3, 29, 23), "v": 1.0}]
        c = tariffs.consumption_by_zone(r, self.G12, ts(2026, 3, 29, 22), ts(2026, 3, 29, 23))
        assert c.kwh_by_zone["nocna"] == pytest.approx(1.0)

    def test_weekend_in_g12w_is_all_cheap(self):
        t = tariffs.resolve_tariff("G12w")
        r = [{"t": ts(2026, 7, 11, 8), "v": 0.0}, {"t": ts(2026, 7, 11, 20), "v": 6.0}]
        c = tariffs.consumption_by_zone(r, t, ts(2026, 7, 11, 8), ts(2026, 7, 11, 20), max_gap_seconds=86400)
        assert c.kwh_by_zone["nocna"] == pytest.approx(6.0) and c.kwh_by_zone["dzienna"] == 0

    def test_unsorted_and_single_reading(self):
        assert tariffs.consumption_by_zone([{"t": 1, "v": 1}], self.G12, 0, 10).total_kwh == 0
        r = [{"t": ts(2026, 7, 8, 11), "v": 1.0}, {"t": ts(2026, 7, 8, 10), "v": 0.0}]
        assert tariffs.consumption_by_zone(r, self.G12, ts(2026, 7, 8, 10), ts(2026, 7, 8, 11)).total_kwh == pytest.approx(1.0)


class TestCost:
    G12 = tariffs.resolve_tariff("G12")

    def _cons(self, day, night):
        return tariffs.Consumption(kwh_by_zone={"dzienna": day, "nocna": night})

    def test_total_price_per_zone_and_vat(self):
        pricing = {"prices": {"dzienna": 1.0, "nocna": 0.5}}
        r = tariffs.compute_cost(self._cons(10, 20), pricing, self.G12, ts(2026, 7, 1), ts(2026, 7, 2))
        assert r["variable_net"] == 20.0 and r["total_net"] == 20.0
        assert r["total_gross"] == pytest.approx(24.6)

    def test_energy_plus_distribution_plus_other(self):
        pricing = {"prices": {"dzienna": {"energy_net": 0.6, "distribution_net": 0.4}, "nocna": 0.3},
                   "other_per_kwh_net": 0.1}
        r = tariffs.compute_cost(self._cons(10, 10), pricing, self.G12, ts(2026, 7, 1), ts(2026, 7, 2))
        assert r["variable_net"] == pytest.approx(10 * 1.1 + 10 * 0.4)

    def test_fixed_fee_prorated_full_month(self):
        pricing = {"prices": {"dzienna": 1, "nocna": 1}, "fixed_monthly_net": 30.0}
        r = tariffs.compute_cost(self._cons(0, 0), pricing, self.G12, ts(2026, 9, 1), ts(2026, 10, 1))
        assert r["fixed_net"] == pytest.approx(30.0)  # wrzesień ma 30 dni

    def test_fixed_fee_one_day(self):
        pricing = {"prices": {}, "fixed_monthly_net": 31.0}
        r = tariffs.compute_cost(self._cons(0, 0), pricing, self.G12, ts(2026, 7, 5), ts(2026, 7, 6))
        assert r["fixed_net"] == pytest.approx(1.0)   # lipiec: 31 dni

    def test_missing_price_is_flagged_not_guessed(self):
        r = tariffs.compute_cost(self._cons(5, 5), {"prices": {"dzienna": 1.0}}, self.G12, ts(2026, 7, 1), ts(2026, 7, 2))
        assert r["missing_prices"] == ["nocna"] and r["variable_net"] == 5.0

    def test_missing_price_for_unused_zone_not_flagged(self):
        r = tariffs.compute_cost(self._cons(5, 0), {"prices": {"dzienna": 1.0}}, self.G12, ts(2026, 7, 1), ts(2026, 7, 2))
        assert r["missing_prices"] == []

    def test_per_tariff_vat_override(self):
        pricing = {"prices": {"dzienna": 1.0, "nocna": 1.0}, "vat_percent": 8}
        r = tariffs.compute_cost(self._cons(10, 0), pricing, self.G12, ts(2026, 7, 1), ts(2026, 7, 2))
        assert r["total_gross"] == pytest.approx(10.8)


class TestPeriods:
    NOW = ts(2026, 10, 10, 15, 30)

    def test_this_month(self):
        assert tariffs.resolve_period("this_month", self.NOW) == (ts(2026, 10, 1), self.NOW)

    def test_last_month(self):
        assert tariffs.resolve_period("last_month", self.NOW) == (ts(2026, 9, 1), ts(2026, 10, 1))

    def test_last_month_across_year(self):
        now = ts(2026, 1, 10)
        assert tariffs.resolve_period("last_month", now) == (ts(2025, 12, 1), ts(2026, 1, 1))

    def test_today_and_yesterday(self):
        assert tariffs.resolve_period("today", self.NOW) == (ts(2026, 10, 10), self.NOW)
        assert tariffs.resolve_period("yesterday", self.NOW) == (ts(2026, 10, 9), ts(2026, 10, 10))

    def test_unknown_period(self):
        with pytest.raises(ValueError):
            tariffs.resolve_period("forever", self.NOW)


class TestSchedule:
    def test_g12w_winter_weekday_blocks(self):
        s = tariffs.describe_schedule(tariffs.resolve_tariff("G12w"))
        assert [(b["od"], b["do"], b["strefa"]) for b in s["zima"]["roboczy"]] == [
            ("00:00", "06:00", "nocna"), ("06:00", "13:00", "dzienna"), ("13:00", "15:00", "nocna"),
            ("15:00", "22:00", "dzienna"), ("22:00", "24:00", "nocna"),
        ]
        assert [b["strefa"] for b in s["zima"]["wolny"]] == ["nocna"]

    def test_g12_weekend_equals_weekday(self):
        s = tariffs.describe_schedule(tariffs.resolve_tariff("G12"))
        assert s["lato"]["roboczy"] == s["lato"]["wolny"]


def _valid_cfg():
    return {
        "vat_percent": 23, "max_gap_minutes": 360,
        "tariffs": {"G12w": {"prices": {"dzienna": 1.0, "nocna": {"energy_net": 0.3, "distribution_net": 0.1}},
                             "fixed_monthly_net": 12.5}},
        "meters": [{"name": "dom", "label": "Dom", "sensor": "licznik_kwh", "tariff": "G12w"}],
    }


class TestValidation:
    def test_valid_config_is_cleaned(self):
        clean, err = tariffs.validate_energy_config(_valid_cfg())
        assert err is None and clean["meters"][0]["scale"] == 1.0
        assert clean["tariffs"]["G12w"]["fixed_monthly_net"] == 12.5

    @pytest.mark.parametrize("mutate,fragment", [
        (lambda c: c.update(vat_percent=150), "VAT"),
        (lambda c: c.update(max_gap_minutes=1), "przerwa"),
        (lambda c: c["tariffs"]["G12w"]["prices"].update(godzinna=1), "nieznana strefa"),
        (lambda c: c["tariffs"]["G12w"]["prices"].update(dzienna=-1), "zakresem"),
        (lambda c: c["tariffs"]["G12w"]["prices"].update(dzienna="abc"), "liczba"),
        (lambda c: c["tariffs"].update(G99={}), "Nieznana taryfa"),
        (lambda c: c["meters"][0].update(tariff="G11"), "nie jest skonfigurowana"),
        (lambda c: c["meters"][0].update(name="zła nazwa!"), "nazwa"),
        (lambda c: c["meters"][0].update(sensor=""), "czujnik"),
        (lambda c: c["meters"][0].update(scale=0), "mnożnik"),
        (lambda c: c["meters"].append(dict(c["meters"][0])), "Zduplikowana"),
        (lambda c: c["tariffs"]["G12w"].update(variant="x"), "wariant"),
    ])
    def test_rejects(self, mutate, fragment):
        cfg = _valid_cfg()
        mutate(cfg)
        clean, err = tariffs.validate_energy_config(cfg)
        assert clean is None and fragment.lower() in err.lower()

    def test_non_dict_rejected(self):
        assert tariffs.validate_energy_config([])[0] is None

    def test_custom_tariff_can_be_used(self):
        cfg = _valid_cfg()
        cfg["custom_tariffs"] = {"MOJA": {"zones": ["a", "b"], "rules": [{"zone": "b", "hours": ["22:00-06:00"]}]}}
        cfg["tariffs"]["MOJA"] = {"prices": {"a": 1, "b": 0.5}}
        assert tariffs.validate_energy_config(cfg)[1] is None


class TestReport:
    def test_report_compares_tariffs_on_same_readings(self):
        cfg, _ = tariffs.validate_energy_config({
            "max_gap_minutes": 1440,
            "tariffs": {
                "G11": {"prices": {"całodobowa": 1.0}},
                "G12w": {"prices": {"dzienna": 1.2, "nocna": 0.6}},
            },
            "meters": [{"name": "dom", "sensor": "s", "tariff": "G12w"}],
        })
        # sobota: całe zużycie w taniej strefie G12w
        r = [{"t": ts(2026, 7, 11, 8), "v": 0.0}, {"t": ts(2026, 7, 11, 20), "v": 12.0}]
        rep = tariffs.build_report(cfg, cfg["meters"][0], r, ts(2026, 7, 11, 8), ts(2026, 7, 11, 20))
        by = {c["tariff"]: c for c in rep["comparison"]}
        assert by["G11"]["total_net"] == 12.0
        assert by["G12w"]["total_net"] == pytest.approx(7.2) and by["G12w"]["active"]
        assert rep["result"]["total_kwh"] == pytest.approx(12.0)
