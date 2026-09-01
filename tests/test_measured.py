"""
Scenarios 10-13 from CLAUDE.md: the pace anchor, and measured mode on the
player's own car.
"""

import pytest

from pit_prediction import PredictionBasis, reference_burn_for_pace

from harness import FERRARI_RBR, Sim, drive_laps, pit_stop, ref


# -- 10. the pace curve anchors on the car, not on the reference constant ----

def test_pace_curve_ignores_a_wrong_reference_lap_time():
    # A reference 3 s off the car's real pace would drag every lap to the
    # push end of the curve if the curve anchored on the row.
    wrong = Sim(reference=ref(baseline_lap_time_s=93.0))
    drive_laps(wrong, 10, lap_time=90.0)
    right = Sim(reference=ref(baseline_lap_time_s=90.0))
    drive_laps(right, 10, lap_time=90.0)

    state = wrong.state()
    assert reference_burn_for_pace(
        wrong.reference, 90.0, state.pace_baseline_s
    ) == wrong.reference.baseline_burn_l_per_lap

    assert (wrong.predict().laps_of_fuel_remaining
            == right.predict().laps_of_fuel_remaining)


def test_one_traffic_lap_does_not_move_the_burn_estimate():
    clean = Sim()
    drive_laps(clean, 10, lap_time=90.0)

    traffic = Sim()
    drive_laps(traffic, 6, lap_time=90.0)
    drive_laps(traffic, 1, lap_time=110.0)   # stuck behind a slower class
    drive_laps(traffic, 3, lap_time=90.0)

    assert traffic.state().rolling_pace_s == 90.0
    assert (traffic.predict().laps_of_fuel_remaining
            == clean.predict().laps_of_fuel_remaining)


# -- 11. measured mode reads the gauge, the shadow inference does not --------

def _player_sim(fuel: float, ncars: int = 1) -> Sim:
    sim = Sim(reference=ref(**FERRARI_RBR), ncars=ncars)
    sim.player_idx = 0
    sim.fuel = fuel
    return sim


def test_partial_load_predicts_from_the_gauge():
    sim = _player_sim(50.0)                      # 50 L in a 104 L tank
    drive_laps(sim, 5, lap_time=89.2, burn_fuel=2.5)

    p = sim.predict()
    assert p.basis is PredictionBasis.MEASURED
    assert p.confidence == 0.95
    assert sim.state().measured_burn_l_per_lap == pytest.approx(2.5)

    expected = (sim.fuel - sim.engine.fuel_reserve_l) / 2.5
    assert abs(p.laps_of_fuel_remaining - expected) < 0.05

    # the shadow inference still tells the full-tank story it would tell
    # without a gauge -- that is what the OWN-CAR CHECK line scores against
    shadow = sim.engine.estimated_fuel_l(0)
    assert shadow > 85.0
    assert shadow > sim.fuel + 40.0


def test_player_fuel_leaves_competitor_rows_untouched():
    with_fuel = _player_sim(50.0, ncars=2)
    drive_laps(with_fuel, 5, lap_time=89.2, burn_fuel=2.5)
    for _ in range(5):
        with_fuel.cars[1]["lap"] += 1
        with_fuel.cars[1]["last_lap"] = 89.2
        with_fuel.tick(0.5)

    without = Sim(reference=ref(**FERRARI_RBR), ncars=2)
    drive_laps(without, 5, lap_time=89.2)
    for _ in range(5):
        without.cars[1]["lap"] += 1
        without.cars[1]["last_lap"] = 89.2
        without.tick(0.5)

    a = with_fuel.engine.predict(1, now=with_fuel.now)
    b = without.engine.predict(1, now=without.now)
    assert a.basis is b.basis
    assert a.laps_of_fuel_remaining == b.laps_of_fuel_remaining
    assert a.confidence == b.confidence


# -- 12. a frozen gauge is a dead feed --------------------------------------

def test_frozen_gauge_drops_measured_mode_but_keeps_the_learned_burn():
    sim = _player_sim(50.0)
    drive_laps(sim, 5, lap_time=89.2, burn_fuel=2.5)
    assert sim.predict().basis is PredictionBasis.MEASURED

    sim.hold(12.0)          # gauge stops moving while RACING
    p = sim.predict()
    assert sim.state().measured_fuel_l is None
    assert p.basis is not PredictionBasis.MEASURED

    # the measured laps EMA-calibrated the factor, so the fallback burn is
    # still the real consumption rather than the reference prior
    fallback_burn = (
        reference_burn_for_pace(sim.reference, 89.2, sim.state().pace_baseline_s)
        * sim.state().personal_burn_factor
    )
    assert abs(fallback_burn - 2.5) < 0.15

    sim.fuel -= 1.0         # a live reading resumes measured mode at once
    sim.tick(0.5)
    assert sim.predict().basis is PredictionBasis.MEASURED


# -- 13. a stop watched on the gauge ----------------------------------------

def test_measured_stop_uses_the_gauge_not_the_stall_clock():
    sim = _player_sim(50.0)
    drive_laps(sim, 5, lap_time=89.2, burn_fuel=2.5)
    entry_fuel = sim.fuel
    burns_before = list(sim.state().measured_burns)

    pit_stop(sim, stall_s=20.0, refuel_to=90.0)

    evt = sim.pit_events[0]
    assert abs(evt.inferred_fuel_added_l - (90.0 - entry_fuel)) < 1.0
    state = sim.state()
    # the anchor is the gauge as the car leaves the lane, a little under the
    # 90 L it was filled to -- and nowhere near a snap to the 104 L tank
    assert state.last_stop_fuel_added_l == pytest.approx(90.0, abs=0.5), \
        "gauge at exit, no full snap"
    assert state.last_stop_fuel_added_l < sim.reference.tank_capacity_l

    # the refuel jump is not a burn sample
    assert sim.state().measured_burns == burns_before
    assert all(b > 0 for b in state.measured_burns)


def test_one_low_burn_lap_does_not_move_the_measured_median():
    sim = _player_sim(60.0)
    drive_laps(sim, 4, lap_time=89.2, burn_fuel=2.5)
    drive_laps(sim, 1, lap_time=95.0, burn_fuel=1.2)   # lift-and-coast in traffic
    drive_laps(sim, 1, lap_time=89.2, burn_fuel=2.5)

    assert sim.state().measured_burn_l_per_lap == pytest.approx(2.5)
