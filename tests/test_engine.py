"""
Baseline regression suite: the scenarios CLAUDE.md documents as "MUST keep
passing". These pin current, believed-correct engine behaviour so that the
race-start repairs can be shown not to disturb anything else.
"""

from pit_prediction import (
    PredictionBasis,
    PitState,
    StopClass,
)

from harness import Sim, drive_laps, drive_through, pit_stop, ref


# -- 1. full stint + 60 s stop ----------------------------------------------

def test_full_stint_then_60s_stop_calibrates():
    sim = Sim()
    # Band width is measured ten laps into each stint: comparing it at the end
    # of a stint would only show that a near-empty tank has nowhere to be wrong.
    drive_laps(sim, 10)
    before = sim.predict()
    band_before = before.predicted_pit_lap_max - before.predicted_pit_lap_min

    drive_laps(sim, 20)
    pit_stop(sim, stall_s=60.0)

    assert len(sim.pit_events) == 1
    evt = sim.pit_events[0]
    assert evt.classified_as is StopClass.FUEL_AND_TYRES
    assert evt.inferred_tyre_change is True
    # 60 s stall - 4 s overhead - 22 s tyres = 34 s of fuel at 2.5 L/s
    assert 80.0 < evt.inferred_fuel_added_l < 90.0

    state = sim.state()
    assert state.stints_observed == 1
    assert 0.9 <= state.personal_burn_factor <= 1.2

    drive_laps(sim, 10)
    after = sim.predict()
    band_after = after.predicted_pit_lap_max - after.predicted_pit_lap_min
    assert after.basis is PredictionBasis.SINGLE_OBSERVATION
    # The band is burn_stddev * (2 - confidence) wide, so an observed stop
    # narrows it. The published figure is truncated to whole laps, which can
    # swallow one stop's worth of tightening -- assert the driver of the width
    # as well as the width itself.
    assert after.confidence > before.confidence
    assert band_after <= band_before
    unc_before = sim.reference.burn_stddev * 2.0
    unc_after = sim.reference.burn_stddev * (2.0 - after.confidence)
    assert unc_after < unc_before


# -- 2. driver swap: fuel clamped to the tank -------------------------------

def test_130s_stop_clamps_fuel_to_tank():
    sim = Sim()
    drive_laps(sim, 20)
    pit_stop(sim, stall_s=130.0)

    evt = sim.pit_events[0]
    assert evt.inferred_fuel_added_l <= sim.reference.tank_capacity_l
    assert sim.state().last_stop_fuel_added_l <= sim.reference.tank_capacity_l


# -- 3. splash ---------------------------------------------------------------

def test_splash_does_not_calibrate_and_does_not_snap_to_full():
    sim = Sim()
    drive_laps(sim, 20)
    leftover = sim.engine.estimated_fuel_l(0)
    pit_stop(sim, stall_s=12.0)

    evt = sim.pit_events[0]
    assert evt.classified_as is StopClass.SPLASH
    assert evt.inferred_tyre_change is False

    state = sim.state()
    assert state.stints_observed == 0, "a splash must never calibrate"
    start_fuel = state.last_stop_fuel_added_l
    assert start_fuel < sim.reference.tank_capacity_l, "must not snap to full"
    assert abs(start_fuel - (leftover + evt.inferred_fuel_added_l)) < 2.0


# -- 4. drive-through --------------------------------------------------------

def test_drive_through_keeps_the_stint_alive():
    sim = Sim()
    drive_laps(sim, 10)
    stint_before = sim.state().stint_number

    drive_through(sim)

    assert len(sim.pit_events) == 1
    assert sim.pit_events[0].classified_as is StopClass.DRIVE_THROUGH
    state = sim.state()
    assert state.stint_number == stint_before, "drive-through is not a new stint"
    assert state.last_pit_lap == 0
    assert state.pit_state is PitState.RACING


# -- 5 / 6. restore ----------------------------------------------------------

def _restored(sim, snapshot, gap_laps, same_session=True):
    """Rebuild an engine from a snapshot and resume `gap_laps` further on."""
    fresh = Sim(reference=sim.reference, start=sim.now)
    fresh.engine.register_competitor(0, cust_id=sim.state().cust_id)
    fresh.engine.import_state(snapshot, same_session=same_session)
    fresh.cars[0]["lap"] = sim.cars[0]["lap"] + gap_laps
    fresh.cars[0]["last_lap"] = 140.0
    fresh.tick(0.5)
    return fresh


def test_same_session_restore_one_lap_gap_reproduces_prediction():
    sim = Sim()
    drive_laps(sim, 12)
    snapshot = sim.engine.export_state()
    baseline = sim.predict()

    fresh = _restored(sim, snapshot, gap_laps=1)
    resumed = fresh.predict()

    # one more lap of fuel gone, but the anchor and burn model are intact
    assert not fresh.state().anchor_uncertain
    assert fresh.state().personal_burn_factor == sim.state().personal_burn_factor
    assert abs(resumed.laps_of_fuel_remaining
               - (baseline.laps_of_fuel_remaining - 1)) < 0.6


def test_long_blackout_reanchors_and_flags_uncertainty():
    sim = Sim()
    drive_laps(sim, 12)
    snapshot = sim.engine.export_state()

    fresh = _restored(sim, snapshot, gap_laps=30)
    fresh.predict()  # predict() is what runs missed-pit inference

    state = fresh.state()
    assert state.anchor_uncertain is True
    assert state.last_pit_lap > 0, "missed-pit inference must re-anchor"

    # a real observed stop clears the uncertainty again
    pit_stop(fresh, stall_s=60.0)
    assert fresh.state().anchor_uncertain is False


# -- 7. lap counter reset ----------------------------------------------------

def test_lap_counter_reset_restarts_stint_but_keeps_burn_factor():
    sim = Sim()
    drive_laps(sim, 30)
    pit_stop(sim, stall_s=60.0)
    factor = sim.state().personal_burn_factor
    assert sim.state().stints_observed == 1

    sim.cars[0]["lap"] = 1
    sim.cars[0]["last_lap"] = 140.0
    sim.tick(0.5)

    state = sim.state()
    assert state.current_lap == 1
    assert state.stint_number == 1
    assert state.last_pit_lap == 0
    assert state.last_stop_fuel_added_l == 0.0
    assert state.personal_burn_factor == factor, "calibration must survive"


# -- 8. race-finish strategy -------------------------------------------------

def test_strategy_stop_counts_at_the_line():
    sim = Sim()
    drive_laps(sim, 17)          # on lap 18, 17 completed
    sim.cars[0]["dist"] = 0.0    # sampled exactly at the line
    sim.tick(0.5)

    assert sim.predict(remaining_s=20 * 60).stops_remaining == 0
    assert sim.predict(remaining_s=60 * 60).stops_remaining == 1
    assert sim.predict(remaining_s=100 * 60).stops_remaining == 1
    assert sim.predict(remaining_s=170 * 60).stops_remaining == 2


# -- 9. field comparison -----------------------------------------------------

def test_compare_to_field_pit_debt_and_undercut():
    sim = Sim(ncars=3)
    for idx in range(3):
        sim.engine.register_competitor(idx, cust_id=100 + idx, class_id="GT3")
    drive_laps(sim, 20, idx=0)
    for idx in (1, 2):
        sim.cars[idx]["lap"] = sim.cars[0]["lap"]
        sim.cars[idx]["last_lap"] = 140.0
    sim.tick(0.5)

    # Everyone stopped two laps ago. We and rival 1 filled to full; rival 2
    # took a short fill and must therefore buy more lane time before the flag.
    lap = sim.state(0).current_lap
    for idx, fill in ((0, 104.0), (1, 104.0), (2, 40.0)):
        st = sim.state(idx)
        st.last_pit_lap = lap - 2
        st.last_stop_fuel_added_l = fill
        st.stints_observed = 1
    # rival 2 sits just behind us on track -- undercut territory
    sim.state(0).last_lap_dist_pct = 0.2
    sim.state(1).last_lap_dist_pct = 0.2
    sim.state(2).last_lap_dist_pct = 0.0

    out = sim.engine.compare_to_field(0, session_time_remaining_s=100 * 60,
                                      now=sim.now)

    assert set(out) == {1, 2}
    assert abs(out[1]["pit_debt_delta_s"]) < 1.0, "same strategy => no debt gap"
    assert out[2]["pit_debt_delta_s"] > 0, "short-filler owes more lane time"
    assert out[2]["undercut_risk"] is True
    assert out[1]["undercut_risk"] is False
