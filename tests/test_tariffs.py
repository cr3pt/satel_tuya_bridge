# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

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
        assert date(2026, 4, 6) in h
        assert date(2026, 6, 4) in h
        assert date(2026, 5, 24) in h

    def test_fixed_holidays(self):
        h = tariffs.polish_holidays(2026)
        for d in [(1, 1), (1, 6), (5, 1), (5, 3), (8, 15), (11, 1), (11, 11), (12, 25), (12, 26)]:
            assert date(2026, *d) in h

    def test_wigilia_is_holiday_only_since_2025(self):
        assert date(2025, 12, 24) in tariffs.polish_holidays(2025)
        assert date(2024, 12, 24) not in tariffs.polish_holidays(2024)

    def test_free_day(self):
        assert tariffs.is_free_day(date(2026, 7, 11))
        assert tariffs.is_free_day(date(2026, 11, 11))
        assert not tariffs.is_free_day(date(2026, 7, 8))


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
        assert at("G12", 2026, 7, 8, 16, variant="pge_sezonowa") == "nocna"
        assert at("G12", 2026, 7, 8, 13, 30, variant="pge_sezonowa") == "dzienna"
        assert at("G12", 2026, 1, 14, 16, variant="pge_sezonowa") == "dzienna"
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
        assert t.zone_at(datetime(2026, 7, 11, 12)) == "tania"

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
        r = [{"t": ts(2026, 7, 8, 10), "v": 0.0}, {"t": ts(2026, 7, 8, 11), "v": 2500.0}]
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


class TestZoneSensors:
    """Tryb "osobny czujnik per strefa" - zużycie liczone wprost z deltą
    KAŻDEGO rejestru, BEZ podziału wg godzin (sam podział już zrobił
    licznik/urządzenie, np. wM-Bus tariff_1/tariff_2_kwh)."""
    G12 = tariffs.resolve_tariff("G12")

    def test_each_zone_uses_its_own_register_regardless_of_time_of_day(self):
        # "dzienna" rośnie w nocy (23:00) - w trybie per-strefa i tak liczy się
        # do "dzienna", bo to rejestr już przypisany do tej strefy przez licznik.
        day = [{"t": ts(2026, 7, 8, 23), "v": 10.0}, {"t": ts(2026, 7, 9, 1), "v": 12.0}]
        night = [{"t": ts(2026, 7, 8, 23), "v": 5.0}, {"t": ts(2026, 7, 9, 1), "v": 5.0}]
        c = tariffs.consumption_from_zone_sensors(
            {"dzienna": day, "nocna": night}, self.G12.zones, ts(2026, 7, 8, 23), ts(2026, 7, 9, 1),
        )
        assert c.kwh_by_zone["dzienna"] == pytest.approx(2.0)
        assert c.kwh_by_zone["nocna"] == pytest.approx(0.0)
        assert c.total_kwh == pytest.approx(2.0)

    def test_missing_zone_readings_default_to_zero(self):
        c = tariffs.consumption_from_zone_sensors({"dzienna": []}, self.G12.zones, ts(2026, 7, 8), ts(2026, 7, 9))
        assert c.kwh_by_zone == {"dzienna": 0.0, "nocna": 0.0}

    def test_scale_applies_per_zone(self):
        day = [{"t": ts(2026, 7, 8, 0), "v": 0.0}, {"t": ts(2026, 7, 8, 1), "v": 2500.0}]
        c = tariffs.consumption_from_zone_sensors(
            {"dzienna": day}, self.G12.zones, ts(2026, 7, 8, 0), ts(2026, 7, 8, 1), scale=0.001,
        )
        assert c.kwh_by_zone["dzienna"] == pytest.approx(2.5)

    def test_gap_in_zone_register_still_counts_to_that_zone(self):
        day = [{"t": ts(2026, 7, 8, 0), "v": 0.0}, {"t": ts(2026, 7, 8, 20), "v": 10.0}]
        c = tariffs.consumption_from_zone_sensors(
            {"dzienna": day}, self.G12.zones, ts(2026, 7, 8), ts(2026, 7, 9), max_gap_seconds=6 * 3600,
        )
        assert c.kwh_by_zone["dzienna"] == pytest.approx(10.0) and c.unallocated_kwh == 0 and c.gaps == 1

    def test_reset_in_one_zone_is_skipped_and_counted(self):
        day = [{"t": ts(2026, 7, 8, 10), "v": 500.0}, {"t": ts(2026, 7, 8, 11), "v": 3.0}]
        c = tariffs.consumption_from_zone_sensors(
            {"dzienna": day}, self.G12.zones, ts(2026, 7, 8, 10), ts(2026, 7, 8, 11),
        )
        assert c.resets == 1 and c.kwh_by_zone["dzienna"] == 0

    def test_daily_breakdown_zone_sensors_sums_to_total(self):
        hours = [0, 4, 8, 12, 16, 20]
        day = [{"t": ts(2026, 7, 8, h), "v": i * 1.0} for i, h in enumerate(hours)]
        night = [{"t": ts(2026, 7, 8, h), "v": i * 0.5} for i, h in enumerate(hours)]
        days = tariffs.daily_breakdown_zone_sensors(
            {"dzienna": day, "nocna": night}, self.G12, {"prices": {"dzienna": 1.0, "nocna": 1.0}},
            ts(2026, 7, 8), ts(2026, 7, 9), max_gap_seconds=6 * 3600, scale=1.0,
        )
        assert len(days) == 1
        assert days[0]["total_kwh"] == pytest.approx(5.0 + 2.5)


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
        assert r["fixed_net"] == pytest.approx(30.0)

    def test_fixed_fee_one_day(self):
        pricing = {"prices": {}, "fixed_monthly_net": 31.0}
        r = tariffs.compute_cost(self._cons(0, 0), pricing, self.G12, ts(2026, 7, 5), ts(2026, 7, 6))
        assert r["fixed_net"] == pytest.approx(1.0)

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


class TestZoneSensorsValidation:
    def test_zone_sensors_without_plain_sensor_is_accepted(self):
        cfg = _valid_cfg()
        cfg["meters"][0]["sensor"] = ""
        cfg["meters"][0]["zone_sensors"] = {"dzienna": "s_dzien", "nocna": "s_noc"}
        clean, err = tariffs.validate_energy_config(cfg)
        assert err is None
        assert clean["meters"][0]["zone_sensors"] == {"dzienna": "s_dzien", "nocna": "s_noc"}
        assert clean["meters"][0]["sensor"] == ""

    def test_unknown_zone_name_is_rejected(self):
        cfg = _valid_cfg()
        cfg["meters"][0]["zone_sensors"] = {"poludniowa": "s1"}
        clean, err = tariffs.validate_energy_config(cfg)
        assert clean is None and "nieznana strefa" in err.lower()

    def test_neither_sensor_nor_zone_sensors_is_rejected(self):
        cfg = _valid_cfg()
        cfg["meters"][0]["sensor"] = ""
        cfg["meters"][0]["zone_sensors"] = {}
        clean, err = tariffs.validate_energy_config(cfg)
        assert clean is None and "czujnik" in err.lower()

    def test_both_sensor_and_zone_sensors_prefers_zone_sensors_cleanly(self):
        cfg = _valid_cfg()
        cfg["meters"][0]["zone_sensors"] = {"dzienna": "s_dzien"}
        clean, err = tariffs.validate_energy_config(cfg)
        assert err is None
        assert clean["meters"][0]["sensor"] == "licznik_kwh"
        assert clean["meters"][0]["zone_sensors"] == {"dzienna": "s_dzien"}

    def test_non_dict_zone_sensors_is_rejected(self):
        cfg = _valid_cfg()
        cfg["meters"][0]["zone_sensors"] = ["dzienna"]
        clean, err = tariffs.validate_energy_config(cfg)
        assert clean is None and "zone_sensors" in err

    def test_meter_without_zone_sensors_key_still_gets_empty_dict(self):
        clean, err = tariffs.validate_energy_config(_valid_cfg())
        assert err is None and clean["meters"][0]["zone_sensors"] == {}


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
        r = [{"t": ts(2026, 7, 11, 8), "v": 0.0}, {"t": ts(2026, 7, 11, 20), "v": 12.0}]
        rep = tariffs.build_report(cfg, cfg["meters"][0], r, ts(2026, 7, 11, 8), ts(2026, 7, 11, 20))
        by = {c["tariff"]: c for c in rep["comparison"]}
        assert by["G11"]["total_net"] == 12.0
        assert by["G12w"]["total_net"] == pytest.approx(7.2) and by["G12w"]["active"]
        assert rep["result"]["total_kwh"] == pytest.approx(12.0)


class TestReportZoneSensors:
    def _cfg(self):
        cfg, err = tariffs.validate_energy_config({
            "max_gap_minutes": 1440,
            "tariffs": {
                "G12": {"prices": {"dzienna": 1.2, "nocna": 0.6}},
                "G12w": {"prices": {"dzienna": 1.0, "nocna": 0.5}},
                "G11": {"prices": {"całodobowa": 1.0}},
            },
            "meters": [{"name": "dom", "tariff": "G12",
                       "zone_sensors": {"dzienna": "s_dzien", "nocna": "s_noc"}}],
        })
        assert err is None
        return cfg

    def test_uses_each_registers_own_delta_regardless_of_time(self):
        cfg = self._cfg()
        zone_readings = {
            "dzienna": [{"t": ts(2026, 7, 11, 23), "v": 0.0}, {"t": ts(2026, 7, 12, 1), "v": 4.0}],
            "nocna": [{"t": ts(2026, 7, 11, 23), "v": 0.0}, {"t": ts(2026, 7, 12, 1), "v": 2.0}],
        }
        rep = tariffs.build_report(
            cfg, cfg["meters"][0], None, ts(2026, 7, 11, 23), ts(2026, 7, 12, 1), zone_readings=zone_readings,
        )
        zones = {z["zone"]: z["kwh"] for z in rep["result"]["zones"]}
        assert zones == {"dzienna": 4.0, "nocna": 2.0}
        assert rep["result"]["total_net"] == pytest.approx(4.0 * 1.2 + 2.0 * 0.6)

    def test_comparison_includes_tariff_with_same_zone_names(self):
        cfg = self._cfg()
        zone_readings = {
            "dzienna": [{"t": ts(2026, 7, 11, 0), "v": 0.0}, {"t": ts(2026, 7, 12, 0), "v": 4.0}],
            "nocna": [{"t": ts(2026, 7, 11, 0), "v": 0.0}, {"t": ts(2026, 7, 12, 0), "v": 2.0}],
        }
        rep = tariffs.build_report(
            cfg, cfg["meters"][0], None, ts(2026, 7, 11), ts(2026, 7, 12), zone_readings=zone_readings,
        )
        by = {c["tariff"]: c for c in rep["comparison"]}
        assert by["G12w"]["total_net"] == pytest.approx(4.0 * 1.0 + 2.0 * 0.5)
        assert "error" not in by["G12w"]

    def test_comparison_flags_tariff_with_different_zones_as_incompatible(self):
        cfg = self._cfg()
        zone_readings = {"dzienna": [{"t": 0, "v": 0.0}, {"t": 3600, "v": 1.0}], "nocna": []}
        rep = tariffs.build_report(cfg, cfg["meters"][0], None, 0, 3600, zone_readings=zone_readings)
        by = {c["tariff"]: c for c in rep["comparison"]}
        assert "error" in by["G11"] and "stref" in by["G11"]["error"].lower()

    def test_meter_field_in_response_reports_zone_sensors(self):
        cfg = self._cfg()
        zone_readings = {"dzienna": [], "nocna": []}
        rep = tariffs.build_report(cfg, cfg["meters"][0], None, 0, 3600, zone_readings=zone_readings)
        assert rep["meter"]["zone_sensors"] == {"dzienna": "s_dzien", "nocna": "s_noc"}
        assert rep["meter"]["sensor"] == ""


class TestCounterBounds:
    def test_start_is_last_reading_before_period_and_end_last_inside(self):
        rows = [{"t": 100.0, "v": 1485.0}, {"t": 200.0, "v": 1490.0}, {"t": 300.0, "v": 1496.006}, {"t": 900.0, "v": 1500.0}]
        b = tariffs.counter_bounds(rows, 150.0, 400.0)
        assert (b["start"], b["end"], b["diff"]) == (1485.0, 1496.006, pytest.approx(11.006))
        assert (b["start_ts"], b["end_ts"]) == (100.0, 300.0)

    def test_without_earlier_reading_start_is_first_inside(self):
        b = tariffs.counter_bounds([{"t": 200.0, "v": 10.0}, {"t": 300.0, "v": 12.0}], 150.0, 400.0)
        assert (b["start"], b["end"]) == (10.0, 12.0)

    def test_no_readings_gives_none(self):
        assert tariffs.counter_bounds([], 0.0, 10.0) is None

    def test_report_includes_counters_per_zone_sensor(self):
        cfg = {"vat_percent": 23, "max_gap_minutes": 120, "custom_tariffs": {},
               "tariffs": {"G12": {"prices": {"dzienna": 1.0, "nocna": 0.5}}}}
        meter = {"name": "dom", "label": "Dom", "tariff": "G12", "scale": 1,
                 "zone_sensors": {"dzienna": "t1", "nocna": "t2"}}
        zr = {"dzienna": [{"t": ts(2026, 7, 8, 0), "v": 1485.0}, {"t": ts(2026, 7, 8, 20), "v": 1496.006}],
              "nocna": [{"t": ts(2026, 7, 8, 0), "v": 349.0}, {"t": ts(2026, 7, 8, 20), "v": 350.219}]}
        rep = tariffs.build_report(cfg, meter, None, ts(2026, 7, 8), ts(2026, 7, 9), zone_readings=zr)
        by = {c["zone"]: c for c in rep["counters"]}
        assert by["dzienna"]["end"] == 1496.006 and by["dzienna"]["sensor"] == "t1"
        assert by["nocna"]["diff"] == pytest.approx(1.219)
        assert rep["result"]["total_kwh"] == pytest.approx(11.006 + 1.219)


INVOICE_G12W = {
    "prices": {"dzienna": {"energy_net": 0.58123, "distribution_net": 0.42760},
               "nocna": {"energy_net": 0.42850, "distribution_net": 0.08450}},
    "per_kwh_fees": {"quality": 0.0332, "cogeneration": 0.003, "oze": 0.0073},
    "monthly_fees": {"trade": 8.48, "capacity": 24.05, "network_fixed": 14.98},
}


class TestInvoiceFees:
    def test_lines_match_invoice(self):
        """Pozycje z faktury G12w 08.10-31.10.2026 (121 kWh dzień, 184 kWh noc, 0,774 m-c)."""
        tariff = tariffs.resolve_tariff("G12w")
        cons = tariffs.Consumption(kwh_by_zone={"dzienna": 121.0, "nocna": 184.0}, samples=2)
        res = tariffs.compute_cost(cons, INVOICE_G12W, tariff, ts(2026, 10, 8), ts(2026, 11, 1))
        lines = {l["label"]: l for l in res["lines"]}
        assert lines["Energia elektryczna dzienna"]["net"] == pytest.approx(70.33)
        assert lines["Energia elektryczna nocna"]["net"] == pytest.approx(78.84)
        assert lines["Składnik zmienny stawki sieciowej dzienna"]["net"] == pytest.approx(51.74)
        assert lines["Składnik zmienny stawki sieciowej nocna"]["net"] == pytest.approx(15.55)
        assert lines["Opłata jakościowa"]["qty"] == pytest.approx(305) and lines["Opłata jakościowa"]["net"] == pytest.approx(10.13)
        assert lines["Opłata kogeneracyjna"]["net"] == pytest.approx(0.92)
        assert lines["Opłata OZE"]["net"] == pytest.approx(2.23)
        assert lines["Opłata handlowa"]["qty"] == pytest.approx(0.774, abs=0.001)
        assert lines["Opłata handlowa"]["net"] == pytest.approx(6.56)
        assert lines["Opłata mocowa"]["net"] == pytest.approx(18.61, abs=0.01)
        assert lines["Składnik stały stawki sieciowej"]["net"] == pytest.approx(11.59, abs=0.01)
        assert sum(l["net"] for l in res["lines"]) == pytest.approx(res["total_net"], abs=0.05)
        assert res["fixed_net"] == pytest.approx((8.48 + 24.05 + 14.98) * 24 / 31, abs=0.01)

    def test_legacy_fields_still_work(self):
        tariff = tariffs.resolve_tariff("G11")
        cons = tariffs.Consumption(kwh_by_zone={"całodobowa": 10.0})
        res = tariffs.compute_cost(cons, {"prices": {"całodobowa": 1.0}, "other_per_kwh_net": 0.1,
                                          "fixed_monthly_net": 31.0}, tariff, ts(2026, 10, 1), ts(2026, 10, 2))
        assert res["variable_net"] == pytest.approx(11.0) and res["fixed_net"] == pytest.approx(1.0)
        assert [l["label"] for l in res["lines"]] == ["Energia i dystrybucja", "Inne opłaty zmienne", "Inne opłaty stałe"]

    def test_validation_keeps_fees_and_rejects_unknown(self):
        cfg = {"tariffs": {"G12w": INVOICE_G12W}, "meters": []}
        clean, err = tariffs.validate_energy_config(cfg)
        assert err is None and clean["tariffs"]["G12w"]["monthly_fees"]["capacity"] == 24.05
        bad = {"tariffs": {"G11": {"prices": {}, "monthly_fees": {"abonament_tv": 5}}}, "meters": []}
        assert "nieznana opłata" in tariffs.validate_energy_config(bad)[1]
        neg = {"tariffs": {"G11": {"prices": {}, "per_kwh_fees": {"oze": -1}}}, "meters": []}
        assert tariffs.validate_energy_config(neg)[1]


class TestMonthlyPeriods:
    def test_resolve_month_period(self):
        now = ts(2026, 11, 21, 7)
        assert tariffs.resolve_period("month:2026-10", now) == (ts(2026, 10, 1), ts(2026, 11, 1))
        assert tariffs.resolve_period("month:2026-11", now) == (ts(2026, 11, 1), now)
        assert tariffs.resolve_period("month:2025-12", now) == (ts(2025, 12, 1), ts(2026, 1, 1))
        for bad in ("month:2026-13", "month:2026-1", "month:2027-01", "month:x"):
            with pytest.raises(ValueError):
                tariffs.resolve_period(bad, now)

    def test_recent_months_crosses_year(self):
        assert tariffs.recent_months(ts(2026, 2, 10), 3) == [(2026, 2), (2026, 1), (2025, 12)]

    def test_monthly_report(self):
        cfg = {"vat_percent": 23, "max_gap_minutes": 120,
               "tariffs": {"G11": {"prices": {"całodobowa": 1.0}, "monthly_fees": {"trade": 10.0}}}}
        meter = {"name": "dom", "tariff": "G11", "sensor": "s"}
        # 1 kWh/h przez całą dobę 30.09 -> 01.10 (przełom miesięcy)
        readings = [{"t": ts(2026, 9, 30) + i * 3600, "v": float(i)} for i in range(49)]
        now = ts(2026, 10, 16)
        rows = tariffs.monthly_report(cfg, meter, readings, tariffs.recent_months(now, 2), now)
        oct_, sep = rows
        assert sep["month"] == "2026-09" and sep["complete"] and sep["total_kwh"] == pytest.approx(24)
        assert sep["fixed_net"] == pytest.approx(10.0) and sep["total_net"] == pytest.approx(34.0)
        assert sep["total_gross"] == pytest.approx(34 * 1.23, abs=0.01) and sep["vat"] == pytest.approx(34 * 0.23, abs=0.01)
        assert oct_["month"] == "2026-10" and not oct_["complete"] and oct_["total_kwh"] == pytest.approx(24)
        assert oct_["fixed_net"] == pytest.approx(10.0 * 15 / 31, abs=0.01)

    def test_monthly_report_skips_months_before_first_reading(self):
        cfg = {"tariffs": {"G11": {"prices": {"całodobowa": 1.0}, "monthly_fees": {"trade": 10.0}}}}
        meter = {"name": "dom", "tariff": "G11", "sensor": "s"}
        now = ts(2026, 10, 16)
        readings = [{"t": ts(2026, 10, 2), "v": 1.0}, {"t": ts(2026, 10, 2, 1), "v": 2.0}]
        rows = tariffs.monthly_report(cfg, meter, readings, tariffs.recent_months(now, 6), now)
        assert [r["month"] for r in rows] == ["2026-10"]
        assert tariffs.monthly_report(cfg, meter, [], tariffs.recent_months(now, 6), now) == []
