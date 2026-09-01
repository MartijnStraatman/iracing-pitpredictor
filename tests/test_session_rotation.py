"""
The other half of the race-start failure: state carried from practice.

`WeekendInfo:SubSessionID` is one number for practice, qualifying AND the race
(run_pit_predictor.py:360). The runner keys `--state` restore on it, so when
the engine is rebuilt at session rotation the practice snapshot still looks
like "the same session" and its stint anchors are restored wholesale.

A practice session ends with the inference showing a nearly empty tank -- the
car has been burning down from its last practice stop for twenty laps. Carried
into the race, that near-zero leftover is what the phantom grid stop adds its
box dwell to, and the result is the 7-40 L the dashboard showed on lap 8.
"""

import pytest

from pit_prediction import (
    TRK_APPROACHING_PITS,
    TRK_IN_PIT_STALL,
    TRK_ON_TRACK,
)

from harness import (
    FERRARI_RBR,
    Sim,
    drive_all_laps,
    pit_stop,
    ref,
)

LAP_TIME = 91.4
TANK = FERRARI_RBR["tank_capacity_l"]
BURN = FERRARI_RBR["baseline_burn_l_per_lap"]
CUST_IDS = (101, 102, 103)
BOX_DWELLS = (36.0, 40.0, 52.0)


def _practice() -> Sim:
    """A normal practice hour: runs, a short fill, more runs."""
    sim = Sim(reference=ref(**FERRARI_RBR), ncars=len(CUST_IDS))
    for i, cid in enumerate(CUST_IDS):
        sim.engine.register_competitor(i, cust_id=cid)
    drive_all_laps(sim, 18, lap_time=LAP_TIME)
    pit_stop(sim, stall_s=14.0, idx=0)          # a short practice fill
    drive_all_laps(sim, 20, lap_time=LAP_TIME)
    return sim


def _race_after(practice: Sim, restore: bool = True) -> Sim:
    """Rebuild the engine the way the runner does at session rotation, then
    run the grid sequence and seven racing laps."""
    sim = Sim(reference=practice.reference, ncars=len(CUST_IDS),
              start=practice.now, start_lap=0)
    for i, cid in enumerate(CUST_IDS):
        sim.engine.register_competitor(i, cust_id=cid)
    if restore:
        # exactly run_pit_predictor.py:1022-1028 -- same SubSessionID, so the
        # practice snapshot is treated as a mid-race recovery
        sim.engine.import_state(practice.engine.export_state(), same_session=True)

    for car in sim.cars:
        car["on_pit"] = True
        car["surface"] = TRK_IN_PIT_STALL
    t, dt = 0.0, 0.5
    while t < max(BOX_DWELLS) + 25.0:
        for i, dwell in enumerate(BOX_DWELLS):
            car = sim.cars[i]
            if t >= dwell and car["surface"] == TRK_IN_PIT_STALL:
                car["surface"] = TRK_APPROACHING_PITS
            elif t >= dwell + 20.0 and car["on_pit"]:
                car["on_pit"] = False
                car["surface"] = TRK_ON_TRACK
        sim.tick(dt)
        t += dt

    for car in sim.cars:
        car["lap"] = 1
    sim.hold(1.0)
    drive_all_laps(sim, 7, lap_time=LAP_TIME)
    return sim


def test_practice_leaves_the_inference_well_down_the_tank():
    """Not a bug on its own -- just the precondition that makes a stale anchor
    poisonous. Documented so the reproduction is readable."""
    practice = _practice()
    for i in range(len(CUST_IDS)):
        assert practice.engine.estimated_fuel_l(i) < 0.5 * TANK


def test_restored_state_does_not_revive_the_grid_stop():
    """The composite failure. A restore repopulates green_laps and the stint
    anchor, so a guard that asks "has this car completed a lap?" by reading
    those counters is answered with a session that is over. The grid stop
    then fires anyway -- and this time the leftover it adds its box dwell to
    is the practice tank, not a full one."""
    practice = _practice()
    race = _race_after(practice)
    assert race.pit_events == [], (
        "the grid was reported as a pit stop despite the pre-green guard: "
        + ", ".join(f"{e.stall_duration_s:.0f}s -> {e.inferred_fuel_added_l:.0f}L"
                    for e in race.pit_events)
    )


def test_race_does_not_inherit_practice_stint_anchors():
    practice = _practice()
    race = _race_after(practice)

    for i in range(len(CUST_IDS)):
        state = race.state(i)
        assert state.stint_number == 1, (
            f"car {i} starts the race on stint {state.stint_number}"
        )
        assert state.last_pit_lap == 0, (
            f"car {i} starts the race anchored to practice lap "
            f"{state.last_pit_lap}"
        )


def test_race_starts_on_a_full_tank_despite_the_practice_snapshot():
    practice = _practice()
    race = _race_after(practice)

    expected = TANK - 7 * BURN          # ~86.5 L on lap 8
    for i in range(len(CUST_IDS)):
        fuel = race.engine.estimated_fuel_l(i)
        assert fuel == pytest.approx(expected, abs=6.0), (
            f"car {i} (sat {BOX_DWELLS[i]:.0f}s in its box) is modelled with "
            f"{fuel:.1f} L on lap 8 instead of {expected:.1f} L"
        )


def test_backwards_restore_gap_drops_the_stale_anchor():
    """`_track_laps` reacts to a restore gap of more than two laps forward but
    silently accepts a jump backwards, which is what session rotation is."""
    practice = _practice()
    sim = Sim(reference=practice.reference, ncars=len(CUST_IDS),
              start=practice.now, start_lap=0)
    for i, cid in enumerate(CUST_IDS):
        sim.engine.register_competitor(i, cust_id=cid)
    sim.engine.import_state(practice.engine.export_state(), same_session=True)

    for car in sim.cars:
        car["lap"] = 1
        car["last_lap"] = LAP_TIME
    sim.tick(0.5)

    state = sim.state(0)
    assert state.current_lap == 1
    assert state.last_pit_lap == 0, (
        "a lap counter that jumped backwards means the anchor is meaningless"
    )
    assert state.last_stop_fuel_added_l == 0.0
