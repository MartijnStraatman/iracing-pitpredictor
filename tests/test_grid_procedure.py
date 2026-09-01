"""
What the engine sees during a real iRacing endurance start.

The procedure, per the user who ran it: cars idle ON THE GRID (on track, not
in the pit box) for up to ~90 s while the grid fills, then a warmup lap behind
the pace car -- sometimes a full lap, sometimes only part of one -- and then
the green flag.

Two variants are exercised, because they differ in what the pit state machine
sees and only one of them can fabricate a stop:

  A. gridded directly -- the car is on track for the whole pre-race phase.
  B. held in the box while the grid fills, then placed on the grid. The
     teleport reads as pit stall -> track, which is a completed pit cycle.

Nothing here asserts which one happened in the race. They exist so the answer
is a test run rather than an assumption.
"""

import pytest

from pit_prediction import (
    FLAG_CAUTION,
    TRK_IN_PIT_STALL,
    TRK_ON_TRACK,
)

from harness import FERRARI_RBR, Sim, drive_all_laps, ref

LAP_TIME = 91.4
PACE_LAP_TIME = 150.0     # a lap behind the pace car is slow
RACE_LAPS = 7             # the screenshot was taken with the field on lap 8
TANK = FERRARI_RBR["tank_capacity_l"]
BURN = FERRARI_RBR["baseline_burn_l_per_lap"]
GRID_IDLE_S = 90.0
BOX_WAITS = (36.0, 55.0, 80.0)


def _sim() -> Sim:
    return Sim(reference=ref(**FERRARI_RBR), ncars=len(BOX_WAITS), start_lap=0)


def _grid_idle(sim: Sim, seconds: float) -> None:
    """Sitting on the grid: on track, stationary, just before the line."""
    for car in sim.cars:
        car["on_pit"] = False
        car["surface"] = TRK_ON_TRACK
        car["dist"] = 0.98
    sim.hold(seconds)


def _pace_lap(sim: Sim, fraction: float = 1.0) -> None:
    """Warmup lap behind the pace car, under caution, then the green."""
    sim.flags = FLAG_CAUTION
    steps = 8
    for k in range(1, steps + 1):
        for car in sim.cars:
            car["dist"] = min(0.999, k / steps)
        sim.tick(PACE_LAP_TIME * fraction / steps)
    for car in sim.cars:
        car["lap"] = 1
        car["dist"] = 0.0
        car["last_lap"] = PACE_LAP_TIME * fraction
    sim.tick(0.5)
    sim.flags = 0


def _variant_a() -> Sim:
    """Gridded directly: never in the pit box at all."""
    sim = _sim()
    _grid_idle(sim, GRID_IDLE_S)
    _pace_lap(sim)
    drive_all_laps(sim, RACE_LAPS, lap_time=LAP_TIME)
    return sim


def _variant_b() -> Sim:
    """Held in the box while the grid fills, then placed on the grid."""
    sim = _sim()
    for car in sim.cars:
        car["on_pit"] = True
        car["surface"] = TRK_IN_PIT_STALL
    t, dt = 0.0, 0.5
    while t < max(BOX_WAITS) + 5.0:
        for i, wait in enumerate(BOX_WAITS):
            if t >= wait and sim.cars[i]["on_pit"]:
                # teleported out of the box onto the grid
                sim.cars[i]["on_pit"] = False
                sim.cars[i]["surface"] = TRK_ON_TRACK
                sim.cars[i]["dist"] = 0.98
        sim.tick(dt)
        t += dt
    _pace_lap(sim)
    drive_all_laps(sim, RACE_LAPS, lap_time=LAP_TIME)
    return sim


@pytest.mark.parametrize("build,name", [(_variant_a, "gridded"),
                                        (_variant_b, "held in box")])
def test_no_stop_is_fabricated_before_the_green(build, name):
    sim = build()
    assert sim.pit_events == [], (
        f"{name}: the start was reported as a pit stop: "
        + ", ".join(f"{e.stall_duration_s:.0f}s -> {e.inferred_fuel_added_l:.0f}L"
                    for e in sim.pit_events)
    )


@pytest.mark.parametrize("build,name", [(_variant_a, "gridded"),
                                        (_variant_b, "held in box")])
def test_field_is_on_a_full_tank_at_the_green(build, name):
    sim = build()
    for i in range(len(BOX_WAITS)):
        state = sim.state(i)
        assert state.last_pit_lap == 0, f"{name}: car {i} is anchored to a stop"
        fuel = sim.engine.estimated_fuel_l(i)
        # the pace lap is a caution lap: it burns, at yellow weighting
        assert fuel == pytest.approx(TANK - RACE_LAPS * BURN, abs=4.0), (
            f"{name}: car {i} modelled with {fuel:.1f} L on lap 8"
        )


def test_partial_pace_lap_is_handled_like_a_full_one():
    """'Sometimes a whole lap, sometimes a part' -- the field is released
    part-way round, so the first counted lap is short."""
    sim = _sim()
    _grid_idle(sim, GRID_IDLE_S)
    _pace_lap(sim, fraction=0.4)
    drive_all_laps(sim, RACE_LAPS, lap_time=LAP_TIME)

    assert sim.pit_events == []
    for i in range(len(BOX_WAITS)):
        fuel = sim.engine.estimated_fuel_l(i)
        assert fuel == pytest.approx(TANK - RACE_LAPS * BURN, abs=4.0)


def test_the_pace_lap_does_not_poison_the_pace_median():
    """A 150 s lap behind the pace car must not be read as a fuel-save lap
    and drag the burn curve for the rest of the race."""
    sim = _sim()
    _grid_idle(sim, GRID_IDLE_S)
    _pace_lap(sim)
    drive_all_laps(sim, RACE_LAPS, lap_time=LAP_TIME)

    assert sim.state(0).rolling_pace_s == pytest.approx(LAP_TIME, abs=0.1)


def test_a_genuine_stop_on_lap_one_is_still_recorded():
    """The guard must not swallow a real early stop -- a car that takes damage
    on the opening lap and pits has genuinely pitted."""
    from harness import pit_stop

    sim = _sim()
    _grid_idle(sim, GRID_IDLE_S)
    _pace_lap(sim)
    drive_all_laps(sim, 2, lap_time=LAP_TIME)   # racing, on lap 3
    pit_stop(sim, stall_s=45.0, idx=0)

    assert len(sim.pit_events) == 1
    assert sim.state(0).last_pit_lap > 0
