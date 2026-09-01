"""
Telemetry synthesis for engine tests.

The engine only ever sees dict frames of CarIdx arrays plus an explicit `now`,
so a whole race can be driven deterministically with no iRacing and no clock.
`Sim` owns the frame dict and the stepped datetime; the module-level verbs
(`drive_laps`, `pit_stop`, `sit_in_box`) move cars through it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pit_prediction import (
    TRK_APPROACHING_PITS,
    TRK_IN_PIT_STALL,
    TRK_ON_TRACK,
    CarTrackReference,
    PitPredictionEngine,
)

# A Ferrari 296 GT3 at Red Bull Ring, from client/references-vrs-s3-rbr.json --
# the configuration the first live race actually ran on.
FERRARI_RBR = dict(
    car_id="ferrari296gt3",
    track_id="spielberg gp",
    tank_capacity_l=104.0,
    baseline_burn_l_per_lap=2.5,
    push_burn_l_per_lap=2.62,
    save_burn_l_per_lap=2.2,
    baseline_lap_time_s=89.2,
    burn_stddev=0.12,
    refuel_rate_l_per_s=2.5,
    tyre_change_time_s=22.0,
    fixed_pit_overhead_s=4.0,
)

# The config CLAUDE.md's documented scenarios are quoted against.
LONG_TRACK = dict(
    car_id="_test_gt3",
    track_id="_test",
    tank_capacity_l=104.0,
    baseline_burn_l_per_lap=3.4,
    push_burn_l_per_lap=3.7,
    save_burn_l_per_lap=3.0,
    baseline_lap_time_s=140.0,
    burn_stddev=0.12,
    refuel_rate_l_per_s=2.5,
    tyre_change_time_s=22.0,
    fixed_pit_overhead_s=4.0,
)


def ref(**overrides) -> CarTrackReference:
    cfg = dict(LONG_TRACK)
    cfg.update(overrides)
    return CarTrackReference(**cfg)


class Sim:
    """Drives an engine with synthetic frames on a stepped clock."""

    def __init__(self, reference: CarTrackReference | None = None, ncars: int = 1,
                 start: datetime | None = None, start_lap: int = 1, **engine_kw):
        self.reference = reference or ref()
        self.engine = PitPredictionEngine(
            reference_provider=lambda _s: self.reference, **engine_kw
        )
        self.now = start or datetime(2026, 8, 23, 12, 0, 0)
        # CarIdxLap is the lap a car is CURRENTLY on: 1 from the green flag,
        # 2 after it first crosses the line, so completed laps = lap - 1.
        # Pre-race cars sitting on the grid report 0 -- pass start_lap=0.
        self.cars = [
            dict(on_pit=False, surface=TRK_ON_TRACK, lap=start_lap, dist=0.0,
                 last_lap=-1.0)
            for _ in range(ncars)
        ]
        self.flags = 0
        self.player_idx = None
        self.fuel = None
        self.pit_events = []
        self.stint_events = []
        self.engine.on_pit_stop(self.pit_events.append)
        self.engine.on_stint(self.stint_events.append)

    # -- frame construction --------------------------------------------------

    def frame(self) -> dict:
        f = {
            "CarIdxOnPitRoad": [c["on_pit"] for c in self.cars],
            "CarIdxTrackSurface": [c["surface"] for c in self.cars],
            "CarIdxLap": [c["lap"] for c in self.cars],
            "CarIdxLastLapTime": [c["last_lap"] for c in self.cars],
            "CarIdxLapDistPct": [c["dist"] for c in self.cars],
            "SessionFlags": self.flags,
        }
        if self.player_idx is not None:
            f["PlayerCarIdx"] = self.player_idx
            f["FuelLevel"] = self.fuel
        return f

    # -- clock ---------------------------------------------------------------

    def tick(self, seconds: float = 0.5) -> None:
        self.now += timedelta(seconds=seconds)
        self.engine.process_frame(self.frame(), now=self.now)

    def hold(self, seconds: float, dt: float = 0.5) -> None:
        """Stay put for `seconds` of wall clock, feeding frames throughout."""
        elapsed = 0.0
        while elapsed < seconds - 1e-9:
            step = min(dt, seconds - elapsed)
            self.tick(step)
            elapsed += step

    # -- convenience ---------------------------------------------------------

    def state(self, idx: int = 0):
        return self.engine.competitors[idx]

    def predict(self, idx: int = 0, remaining_s: float | None = None):
        return self.engine.predict(idx, remaining_s, now=self.now)


def drive_laps(sim: Sim, n: int, lap_time: float = 140.0, idx: int = 0,
               steps: int = 6, burn_fuel: float | None = None) -> None:
    """Complete `n` green laps, advancing LapDistPct through each one."""
    car = sim.cars[idx]
    for _ in range(n):
        for k in range(1, steps + 1):
            car["dist"] = min(0.999, k / steps)
            if burn_fuel is not None and sim.fuel is not None:
                sim.fuel -= burn_fuel / steps
            sim.tick(lap_time / steps)
        car["lap"] += 1
        car["dist"] = 0.0
        car["last_lap"] = lap_time
        sim.tick(0.5)


def _crawl(sim: Sim, seconds: float, dt: float = 0.5) -> None:
    """Hold, but keep the fuel gauge ticking down as a running engine would.

    A perfectly constant reading while moving is what the engine treats as a
    dead feed, so pit-lane travel has to burn something or measured mode drops
    on the way out of the box.
    """
    elapsed = 0.0
    while elapsed < seconds - 1e-9:
        step = min(dt, seconds - elapsed)
        if sim.fuel is not None:
            sim.fuel -= 0.01 * step
        sim.tick(step)
        elapsed += step


def drive_all_laps(sim: Sim, n: int, lap_time: float = 140.0,
                   steps: int = 6) -> None:
    """Complete `n` green laps for every car at once, on one shared clock."""
    for _ in range(n):
        for k in range(1, steps + 1):
            for car in sim.cars:
                car["dist"] = min(0.999, k / steps)
            sim.tick(lap_time / steps)
        for car in sim.cars:
            car["lap"] += 1
            car["dist"] = 0.0
            car["last_lap"] = lap_time
        sim.tick(0.5)


def pit_stop(sim: Sim, stall_s: float, idx: int = 0, entry_s: float = 15.0,
             exit_s: float = 15.0, refuel_to: float | None = None) -> None:
    """Full pit cycle: pit road -> stall -> pit road -> track."""
    car = sim.cars[idx]
    car["on_pit"] = True
    car["surface"] = TRK_APPROACHING_PITS
    _crawl(sim, entry_s)
    car["surface"] = TRK_IN_PIT_STALL
    sim.hold(stall_s)
    if refuel_to is not None:
        sim.fuel = refuel_to
        sim.tick(0.5)
    car["surface"] = TRK_APPROACHING_PITS
    _crawl(sim, exit_s)
    car["on_pit"] = False
    car["surface"] = TRK_ON_TRACK
    _crawl(sim, 1.0)


def drive_through(sim: Sim, idx: int = 0, seconds: float = 20.0) -> None:
    """Down pit road and back out without ever stopping in the stall."""
    car = sim.cars[idx]
    car["on_pit"] = True
    car["surface"] = TRK_APPROACHING_PITS
    sim.hold(seconds)
    car["on_pit"] = False
    car["surface"] = TRK_ON_TRACK
    sim.hold(1.0)


def sit_in_box(sim: Sim, seconds: float, idx: int = 0) -> None:
    """Pre-race grid: parked in the pit stall, engine running, lap 0."""
    car = sim.cars[idx]
    car["on_pit"] = True
    car["surface"] = TRK_IN_PIT_STALL
    sim.hold(seconds)


def leave_box(sim: Sim, idx: int = 0, exit_s: float = 20.0) -> None:
    """Pull out of the box and down the pit lane onto the track."""
    car = sim.cars[idx]
    car["surface"] = TRK_APPROACHING_PITS
    sim.hold(exit_s)
    car["on_pit"] = False
    car["surface"] = TRK_ON_TRACK
    sim.hold(1.0)
