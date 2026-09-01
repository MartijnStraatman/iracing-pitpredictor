"""
The failure that ruined the first live race (RBR 3 h, 2026-08-29).

`CarIdxTrackSurface == NOT_IN_WORLD` means two unrelated things: a car being
towed, and a car that simply is not in the session — sitting in the garage,
not yet joined, or disconnected. iRacing reports `CarIdxLap == -1` for the
latter.

Read as a tow, the state machine walked RACING → ENTERING → IN_STALL →
EXITING → RACING and closed a stint every cycle, because `EXITING → RACING`
only asked `not on_pit` and a car out of the world is not on pit road. The
cycle ran at about 1 Hz for as long as the car sat in its garage. Each pass:

  * emitted a phantom `PitStopEvent` ("pitted for 1s -- repair or tow"),
  * anchored `last_pit_lap = -1`, which adds a permanent extra lap of burn to
    every later estimate for that car (`current_lap - (-1)`),
  * classified DAMAGE, so `start_fuel = leftover` with no fill-to-full snap
    and no calibration — leaving the car on PRIOR_ONLY for the whole race,
  * and re-anchored fuel one notch lower each time, compounding.

Measured from the recording: car #6 fell 106 L → 17 L in seven seconds while
stationary in the garage. 20 of 60 cars carried a negative anchor into the
race; the dashboard sorts by fuel ascending, so all of them sat at the top of
the timing tower.
"""

import pytest

from pit_prediction import (
    TRK_IN_PIT_STALL,
    TRK_NOT_IN_WORLD,
    TRK_ON_TRACK,
)

from harness import FERRARI_RBR, Sim, drive_laps, ref

TANK = FERRARI_RBR["tank_capacity_l"]
BURN = FERRARI_RBR["baseline_burn_l_per_lap"]
LAP_TIME = 91.4


def _to_garage(sim: Sim, seconds: float, idx: int = 0) -> None:
    """The car leaves the session: out of the world, lap reported as -1."""
    sim.cars[idx]["surface"] = TRK_NOT_IN_WORLD
    sim.cars[idx]["on_pit"] = False
    sim.cars[idx]["lap"] = -1
    sim.hold(seconds)


def _racing_sim() -> Sim:
    sim = Sim(reference=ref(**FERRARI_RBR))
    drive_laps(sim, 3, lap_time=LAP_TIME)
    return sim


def test_garage_does_not_emit_phantom_stops():
    sim = _racing_sim()
    _to_garage(sim, 30.0)

    assert sim.pit_events == [], (
        f"{len(sim.pit_events)} phantom stop(s) emitted while the car sat in "
        f"the garage for 30 s"
    )


def test_garage_does_not_drain_the_tank():
    sim = _racing_sim()
    before = sim.engine.estimated_fuel_l(0)
    _to_garage(sim, 30.0)
    after = sim.engine.estimated_fuel_l(0)

    assert after == pytest.approx(before, abs=1.0), (
        f"a stationary car in the garage burned {before - after:.1f} L in 30 s"
    )


def test_anchor_never_goes_negative():
    sim = _racing_sim()
    _to_garage(sim, 30.0)
    assert sim.state().last_pit_lap >= 0


def test_the_car_recovers_when_it_rejoins():
    """Out-of-world is a blind spot, not a state change: the car comes back
    to the same stint it left."""
    sim = _racing_sim()
    fuel_before = sim.engine.estimated_fuel_l(0)
    stint_before = sim.state().stint_number

    _to_garage(sim, 30.0)
    sim.cars[0]["surface"] = TRK_ON_TRACK       # back in the world, same lap
    sim.cars[0]["lap"] = sim.state().current_lap
    sim.hold(2.0)

    state = sim.state()
    assert state.stint_number == stint_before
    assert state.pit_state.value == "RACING"
    # the blind spot cost the car nothing: it burned no fuel sitting still
    assert sim.engine.estimated_fuel_l(0) == pytest.approx(fuel_before, abs=0.5)


def test_a_real_tow_is_still_detected():
    """A car that is racing, drops out of the world, and reappears in its pit
    stall has genuinely been towed — that must still close the stint."""
    sim = _racing_sim()

    sim.cars[0]["surface"] = TRK_NOT_IN_WORLD   # picked up, still on lap 3
    sim.hold(4.0)
    sim.cars[0]["surface"] = TRK_IN_PIT_STALL   # deposited in the box
    sim.cars[0]["on_pit"] = True
    sim.hold(40.0)
    sim.cars[0]["surface"] = TRK_ON_TRACK       # repaired, released
    sim.cars[0]["on_pit"] = False
    sim.hold(2.0)

    assert len(sim.pit_events) == 1
    assert sim.pit_events[0].classified_as.value == "DAMAGE"
    assert sim.state().last_pit_lap == 4   # three laps completed, on lap 4


def test_never_joined_car_is_left_alone():
    """A car registered from the roster that never appears on track must not
    accumulate anything at all."""
    sim = Sim(reference=ref(**FERRARI_RBR), start_lap=-1)
    sim.engine.register_competitor(0, cust_id=99)
    sim.cars[0]["surface"] = TRK_NOT_IN_WORLD
    sim.cars[0]["on_pit"] = False
    sim.hold(60.0)

    state = sim.state()
    assert sim.pit_events == []
    assert state.last_pit_lap == 0
    assert state.stint_number == 1
    assert state.pit_state.value == "RACING"
