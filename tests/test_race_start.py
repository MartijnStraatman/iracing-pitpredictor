"""
Reproduction of the first live race (Red Bull Ring, 3 h, 60 cars).

The dashboard showed the whole field with correct pace, correct burn rate and
correct tank capacity, but fuel on board 3-5x too low, scattered differently
per car, everything on PRIOR_ONLY basis and never self-correcting. These tests
drive the sequence that produces it: cars parked in their pit boxes before the
green flag, each for a different length of time.

They assert the behaviour the engine SHOULD have. Until the repair lands they
fail, and the failure is the reproduction.
"""

import pytest

from pit_prediction import (
    TRK_APPROACHING_PITS,
    TRK_IN_PIT_STALL,
    TRK_ON_TRACK,
)

from harness import FERRARI_RBR, Sim, drive_all_laps, ref

# Seconds each car sat in its box before pulling out for the formation lap.
# Back-solved from the screenshot: these dwells, read as refuel time, produce
# exactly the stint-start fuel loads the dashboard was working from.
BOX_DWELLS = (36.0, 40.0, 52.0)

LAP_TIME = 91.4          # Moldovan Drivers' pace, from pit_eta / laps_of_fuel
RACE_LAPS = 7            # the field was on lap 8 when the screenshot was taken
TANK = FERRARI_RBR["tank_capacity_l"]
BURN = FERRARI_RBR["baseline_burn_l_per_lap"]


LANE_TIME = 20.0         # box exit to the end of pit road


def _grid_start() -> Sim:
    """Pre-race grid -> formation lap -> green -> seven racing laps."""
    sim = Sim(reference=ref(**FERRARI_RBR), ncars=len(BOX_DWELLS), start_lap=0)
    for car in sim.cars:
        car["on_pit"] = True
        car["surface"] = TRK_IN_PIT_STALL

    # Each car pulls out of its box when its own dwell expires, drives down
    # the lane, and joins the track for the formation lap.
    t, dt = 0.0, 0.5
    while t < max(BOX_DWELLS) + LANE_TIME + 5.0:
        for i, dwell in enumerate(BOX_DWELLS):
            car = sim.cars[i]
            if t >= dwell and car["surface"] == TRK_IN_PIT_STALL:
                car["surface"] = TRK_APPROACHING_PITS
            elif t >= dwell + LANE_TIME and car["on_pit"]:
                car["on_pit"] = False
                car["surface"] = TRK_ON_TRACK
        sim.tick(dt)
        t += dt

    # green flag: the field is on lap 1
    for car in sim.cars:
        car["lap"] = 1
    sim.hold(1.0)
    drive_all_laps(sim, RACE_LAPS, lap_time=LAP_TIME)
    return sim


def test_no_pit_stop_is_emitted_before_the_green_flag():
    sim = _grid_start()
    assert sim.pit_events == [], (
        "sitting on the grid was reported to the team as a pit stop: "
        + ", ".join(f"{e.stall_duration_s:.0f}s -> {e.inferred_fuel_added_l:.0f}L"
                    for e in sim.pit_events)
    )


def test_grid_cars_start_the_race_on_a_full_tank():
    sim = _grid_start()
    for idx in range(len(BOX_DWELLS)):
        fuel = sim.engine.estimated_fuel_l(idx)
        expected = TANK - RACE_LAPS * BURN     # ~86.5 L of a 104 L tank
        assert fuel == pytest.approx(expected, abs=6.0), (
            f"car {idx} (sat {BOX_DWELLS[idx]:.0f}s in its box) is modelled "
            f"with {fuel:.1f} L on lap 8, not {expected:.1f} L"
        )


def test_the_field_is_not_predicted_to_pit_within_three_laps():
    sim = _grid_start()
    for idx in range(len(BOX_DWELLS)):
        p = sim.predict(idx, remaining_s=2 * 3600 + 48 * 60)
        assert p.laps_of_fuel_remaining > 30.0, (
            f"car {idx} shown with {p.laps_of_fuel_remaining:.1f} laps of fuel "
            f"on lap 8 of a full tank"
        )
        assert p.stops_remaining == 2, (
            f"car {idx} shown needing {p.stops_remaining} stops"
        )


def test_the_error_is_not_the_same_for_every_car():
    """The tell that this is box dwell and not a constant offset: each car is
    wrong by a different amount, in proportion to how long it sat."""
    sim = _grid_start()
    fuels = [sim.engine.estimated_fuel_l(i) for i in range(len(BOX_DWELLS))]
    assert max(fuels) - min(fuels) < 1.0, (
        f"cars that sat different lengths of time hold different fuel: {fuels}"
    )
