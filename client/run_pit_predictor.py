"""
Live pit prediction runner for iRacing.

Connects to iRacing via pyirsdk, feeds telemetry into PitPredictionEngine,
and renders a live console table of predicted pit stops for every car.

Usage (on the iRacing PC):
    pip install pyirsdk
    python run_pit_predictor.py --refs references.json

    # without iRacing running, verify the display with simulated data:
    python run_pit_predictor.py --demo

references.json format (list of reference rows, matched on car_id + track_id):
[
  {
    "car_id": "ferrari296gt3",
    "track_id": "spa grandprix",
    "tank_capacity_l": 104.0,
    "baseline_burn_l_per_lap": 3.4,
    "baseline_lap_time_s": 140.0,
    "push_burn_l_per_lap": 3.7,
    "save_burn_l_per_lap": 3.1,
    "burn_stddev": 0.1,
    "refuel_rate_l_per_s": 2.5,
    "tyre_change_time_s": 22.0,
    "fixed_pit_overhead_s": 4.0
  }
]

Both ids are matched EXACTLY, case-sensitively, against the session YAML:
  car_id   == DriverInfo:Drivers[i]:CarPath   e.g. "ferrari296gt3"
  track_id == WeekendInfo:TrackName           e.g. "spa grandprix", "spielberg gp"
TrackName is the track folder plus its config, lowercase and space-separated
-- not TrackDisplayName ("Circuit de Spa-Francorchamps", "Red Bull Ring") and
not the short name. To read both off a live session:

    python -c "import irsdk;ir=irsdk.IRSDK();ir.startup();\
print(repr(ir['WeekendInfo']['TrackName']));\
print(sorted({d['CarPath'] for d in ir['DriverInfo']['Drivers']}))"

Every row is a complete standalone entry for one (car, track) pair; there
are no wildcard/fallback rows. A row you need but do not have is reported in
the event log at startup -- an unmatched car falls back to generic GT3
numbers, which is a ~30% error on every prediction for it, so the misses are
worth reading.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from pit_prediction import (
    CarTrackReference,
    reference_burn_for_pace,
    CompetitorState,
    PitPrediction,
    PitPredictionEngine,
    PitStopEvent,
    StintEvent,
    TRK_IN_PIT_STALL,
    TRK_ON_TRACK,
)

POLL_HZ = 10
DISPLAY_EVERY_S = 1.0

# Two-driver team setup: the client runs on BOTH drivers' PCs so real
# FuelLevel follows whoever is in the car. Every uplink payload is stamped
# with this process id + whether OUR member is (recently) driving; the relay
# prefers the active driver's feed and drops the passive one.
CLIENT_ID = uuid.uuid4().hex[:12]
DRIVER_ACTIVE_HOLD_S = 60.0  # stay "active" this long after leaving the car


# ---------------------------------------------------------------------------
# Relay uplink (background thread, stdlib only)
# ---------------------------------------------------------------------------

class Uplink:
    """
    Pushes snapshots/events to the relay server without ever blocking the
    telemetry loop. Failures are silent-but-visible (status string) and the
    latest snapshot always wins -- stale ones are dropped, not queued.
    """

    def __init__(self, url: str, token: str):
        import queue
        import threading
        self.url = url.rstrip("/") + "/api/ingest"
        self.token = token
        self.q: "queue.Queue" = queue.Queue(maxsize=200)
        self.status = "starting"
        self.driver_active = False  # set by the main loop from IsOnTrack
        t = threading.Thread(target=self._worker, daemon=True)
        t.start()

    def send_snapshot(self, payload: dict) -> None:
        # drop any queued snapshot; only the newest matters
        try:
            items = []
            while True:
                item = self.q.get_nowait()
                if item.get("type") != "snapshot":
                    items.append(item)
        except Exception:
            pass
        for item in items:
            self._put(item)
        self._put(payload)

    def send_event(self, message: str) -> None:
        self._put({
            "type": "event",
            "message": message,
            "ts": datetime.utcnow().isoformat() + "Z",
        })

    def _put(self, item: dict) -> None:
        # every payload carries who sent it and whether our member is driving,
        # so the relay can arbitrate between the two team PCs' clients
        item.setdefault("client_id", CLIENT_ID)
        item.setdefault("driver_active", self.driver_active)
        try:
            self.q.put_nowait(item)
        except Exception:
            pass  # queue full: shed load, never block

    def _worker(self) -> None:
        import urllib.request
        while True:
            item = self.q.get()
            req = urllib.request.Request(
                self.url,
                data=json.dumps(item).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.token}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=4) as resp:
                    self.status = "connected" if resp.status == 200 else f"http {resp.status}"
            except Exception as exc:
                self.status = f"offline ({type(exc).__name__})"
                time.sleep(2)  # back off, don't hammer a dead server


# ---------------------------------------------------------------------------
# Reference table loading
# ---------------------------------------------------------------------------

# The numbers a row has to carry to be worth anything. Every field left unset
# silently takes a CarTrackReference default -- generic GT3, 100 L, 2.8 L/lap
# at a 120 s lap -- and the engine then predicts from it with a straight face.
REQUIRED_REF_FIELDS = (
    "tank_capacity_l",
    "baseline_burn_l_per_lap",
    "push_burn_l_per_lap",
    "save_burn_l_per_lap",
)


def load_references(
    path: Optional[str],
    on_warn: Optional[Callable[[str], None]] = None,
) -> Tuple[Dict[str, CarTrackReference], Dict[str, List[str]]]:
    """
    Load reference rows keyed by 'car_id|track_id'.

    Every row is standalone: one complete entry per (car, track) pair,
    carrying the car numbers (tank_capacity_l, refuel_rate_l_per_s,
    tyre_change_time_s) alongside the track numbers (burn rates, lap time).
    There is no wildcard/inheritance -- burn per lap is a track property
    (Le Mans and Lime Rock differ by 3x), so a car-level burn number was
    never meaningful, and a (car, track) pair without a row now fails loudly
    instead of quietly running on a car-level guess.

    Returns (references, gaps), where gaps maps the same keys to the
    REQUIRED_REF_FIELDS the file never set for that row -- reported by the
    provider so a half-filled row cannot pass for a measured one.

    Unrecognised keys are still ignored rather than fatal, but they are
    reported: a misspelt field is indistinguishable from an absent one, and
    silently reverting to a default is how a reference file lies. Prefix a key
    with "_" to annotate a row without tripping the check.
    """
    refs: Dict[str, CarTrackReference] = {}
    gaps: Dict[str, List[str]] = {}
    if not path:
        return refs, gaps
    data = json.loads(Path(path).read_text())
    valid = {f.name for f in CarTrackReference.__dataclass_fields__.values()}
    unknown = sorted(
        {k for row in data for k in row if k not in valid and not k.startswith("_")}
    )
    if unknown and on_warn:
        on_warn(
            f"WARNING: {path} has unrecognised field(s) {', '.join(unknown)} -- "
            f"ignored, so those rows are using defaults. Check the spelling."
        )
    rows = [{k: v for k, v in row.items() if k in valid} for row in data]
    for i, row in enumerate(rows):
        if "car_id" not in row or "track_id" not in row:
            raise ValueError(f"{path}: row {i} needs both car_id and track_id")
        if row["track_id"] == "*":
            raise ValueError(
                f"{path}: wildcard rows (track_id \"*\") are no longer "
                f"supported -- give '{row['car_id']}' one complete row per "
                f"track instead (copy the tank/refuel/tyre numbers into each)."
            )

    for row in rows:
        ref = CarTrackReference(**row)
        key = f"{ref.car_id}|{ref.track_id}"
        refs[key] = ref
        missing = [f for f in REQUIRED_REF_FIELDS if f not in row]
        if missing:
            gaps[key] = missing
    return refs, gaps


def make_reference_provider(
    refs: Dict[str, CarTrackReference],
    track_id: str,
    gaps: Optional[Dict[str, List[str]]] = None,
    on_warn: Optional[Callable[[str], None]] = None,
):
    """
    Resolve a car's reference row by exact car+track match -- nothing else.

    A miss used to return None and say nothing, which is the worst way for
    this to fail: the engine falls back to generic GT3 numbers and keeps
    predicting at full confidence, reading ~30% off for the rest of the race.
    Every distinct problem is now reported once through `on_warn`, quoting the
    exact track_id string to paste into references.json.
    """
    gaps = gaps or {}
    default = PitPredictionEngine.DEFAULT_REFERENCE
    warned: set = set()

    def warn(msg: str) -> None:
        if on_warn and msg not in warned:
            warned.add(msg)
            on_warn(msg)

    def provider(state: CompetitorState) -> Optional[CarTrackReference]:
        car = state.car_id
        if not car:
            return None  # roster not loaded yet; asked again on the next refresh
        key = f"{car}|{track_id}"
        if key not in refs:
            warn(
                f"WARNING: no reference row for '{car}' at '{track_id}' -- "
                f"falling back to generic GT3 ({default.tank_capacity_l:.0f}L, "
                f"{default.baseline_burn_l_per_lap} L/lap). Add a row with "
                f'"car_id": "{car}", "track_id": "{track_id}".'
            )
            return None
        if gaps.get(key):
            warn(
                f"WARNING: reference '{key}' never sets "
                f"{', '.join(gaps[key])} -- those take generic defaults and "
                f"skew every prediction for this car."
            )
        return refs[key]

    return provider


# ---------------------------------------------------------------------------
# Telemetry sources
# ---------------------------------------------------------------------------

class IRacingSource:
    """Wraps pyirsdk: connection handling, session YAML, frame snapshots."""

    def __init__(self):
        import irsdk  # pyirsdk
        self.ir = irsdk.IRSDK()
        self.connected = False

    def ensure_connected(self) -> bool:
        if self.connected and not (self.ir.is_initialized and self.ir.is_connected):
            self.connected = False
            self.ir.shutdown()
        if not self.connected and self.ir.startup() and self.ir.is_connected:
            self.connected = True
        return self.connected

    def frame(self) -> Optional[dict]:
        if not self.ensure_connected():
            return None
        self.ir.freeze_var_buffer_latest()
        try:
            return {
                "CarIdxOnPitRoad": list(self.ir["CarIdxOnPitRoad"] or []),
                "CarIdxTrackSurface": list(self.ir["CarIdxTrackSurface"] or []),
                "CarIdxLap": list(self.ir["CarIdxLap"] or []),
                "CarIdxLastLapTime": list(self.ir["CarIdxLastLapTime"] or []),
                "CarIdxLapDistPct": list(self.ir["CarIdxLapDistPct"] or []),
                "CarIdxPosition": list(self.ir["CarIdxPosition"] or []),
                "CarIdxClassPosition": list(self.ir["CarIdxClassPosition"] or []),
                "SessionFlags": self.ir["SessionFlags"] or 0,
                "SessionTimeRemain": self.ir["SessionTimeRemain"],
                "SessionNum": self.ir["SessionNum"],
                "FuelLevel": self.ir["FuelLevel"],
                "PlayerCarIdx": self.ir["PlayerCarIdx"],
                "IsOnTrack": bool(self.ir["IsOnTrack"]),
            }
        finally:
            self.ir.unfreeze_var_buffer_latest()

    def session_info(self) -> dict:
        """Driver roster + track from the session YAML. {} if unavailable."""
        try:
            weekend = self.ir["WeekendInfo"] or {}
            driver_info = self.ir["DriverInfo"] or {}
            drivers = driver_info.get("Drivers", [])
            # Player's BoP-effective tank: physical capacity x max fuel percent.
            # Ground truth for validating the reference table's tank_capacity_l.
            eff_tank = None
            try:
                eff_tank = float(driver_info.get("DriverCarFuelMaxLtr", 0)) * float(
                    driver_info.get("DriverCarMaxFuelPct", 1.0)
                )
            except (TypeError, ValueError):
                pass
            player_idx = driver_info.get("DriverCarIdx")
            player_car = next(
                (d.get("CarPath", "") for d in drivers if d.get("CarIdx") == player_idx),
                "",
            )
            # human-readable session type (Practice / Qualifying / Race)
            session_type = ""
            try:
                snum = self.ir["SessionNum"]
                sessions = (self.ir["SessionInfo"] or {}).get("Sessions", [])
                session_type = str(sessions[snum].get("SessionType", "")) if snum is not None else ""
            except Exception:
                pass
            return {
                "track_id": str(weekend.get("TrackName", "unknown")),
                "track_display": str(weekend.get("TrackDisplayName", "") or weekend.get("TrackName", "")),
                "session_type": session_type,
                "effective_tank_l": eff_tank,
                "player_car_id": player_car,
                "player_car_idx": player_idx,
                # SubSessionID uniquely identifies this race instance -- it
                # stays the same if the client crashes and rejoins, so it is
                # the key for deciding full vs. factors-only state restore.
                "session_id": str(
                    weekend.get("SubSessionID") or weekend.get("SessionID", "")
                ),
                "drivers": [
                    {
                        "car_idx": d.get("CarIdx"),
                        "cust_id": d.get("UserID", -1),
                        "team_id": d.get("TeamID", 0),
                        "name": d.get("TeamName") or d.get("UserName", f"Car {d.get('CarIdx')}"),
                        "car_id": d.get("CarPath", ""),
                        "car_name": d.get("CarScreenNameShort")
                        or d.get("CarScreenName")
                        or d.get("CarPath", ""),
                        "car_number": d.get("CarNumber", ""),
                        "class_id": str(d.get("CarClassID", "")),
                        "is_pace_car": d.get("CarIsPaceCar", 0) == 1,
                    }
                    for d in drivers
                ],
            }
        except Exception:
            return {}


DEMO_CAR_NAMES = {
    "acuransxevo22gt3": "Acura NSX GT3 EVO 22",
    "amvantageevogt3": "Aston Martin Vantage GT3 EVO",
    "audir8lmsevo2gt3": "Audi R8 LMS EVO II GT3",
    "bmwm4gt3": "BMW M4 GT3",
    "bmwm4gt3evo": "BMW M4 GT3 EVO",
    "chevyvettez06rgt3": "Chevrolet Corvette Z06 GT3.R",
    "ferrari296gt3": "Ferrari 296 GT3",
    "fordmustanggt3": "Ford Mustang GT3",
    "lamborghinievogt3": "Lamborghini Huracan GT3 EVO",
    "mclaren720sgt3": "McLaren 720S GT3 EVO",
    "mercedesamgevogt3": "Mercedes-AMG GT3 2020",
    "porsche992rgt3": "Porsche 911 GT3 R",
}


class DemoSource:
    """Simulated session so the display can be tested without iRacing.

    Given loaded references, the grid is built FROM the file: one car per
    reference row, using that row's real car_id, tank, burn and lap time --
    so the engine races the same field the reference file describes.
    Without references it falls back to a built-in 3-Ferrari sample."""

    def __init__(self, refs: Optional[Dict[str, CarTrackReference]] = None):
        self.t0 = time.monotonic()
        self.speedup = 60  # 1 real second = 1 simulated minute
        self._epoch = datetime.utcnow()
        self.track_id = "demo_spa"
        self.track_display = "Circuit de Spa-Francorchamps (Demo)"
        self.player_idx = 0
        rows = []
        if refs:
            rows = sorted(
                (r for r in refs.values()
                 if not r.car_id.startswith(("VERIFY", "FILL"))),
                key=lambda r: r.car_id,
            )
        if rows:
            self.track_id = rows[0].track_id
            self.track_display = f"{rows[0].track_id} (demo from references)"
            self.player_idx = next(
                (i for i, r in enumerate(rows)
                 if r.car_id == "mercedesamgevogt3"), 0)
            self.cars = []
            for i, r in enumerate(rows):
                # spread the field a little and stagger the pit windows so
                # the tower shuffles and stops don't all land on one lap
                laps_on_tank = int(r.tank_capacity_l
                                   / r.baseline_burn_l_per_lap * 0.94)
                self.cars.append({
                    "lap_time": r.baseline_lap_time_s + (i % 5) * 0.35,
                    "pit_on_lap": max(3, laps_on_tank - (i % 4)),
                    "stop_s": r.fixed_pit_overhead_s + r.tyre_change_time_s
                    + 0.9 * r.tank_capacity_l / r.refuel_rate_l_per_s,
                    "car_id": r.car_id,
                    "car_name": DEMO_CAR_NAMES.get(r.car_id, r.car_id),
                    "tank": r.tank_capacity_l,
                    "burn": r.baseline_burn_l_per_lap,
                })
        else:
            self.cars = [
                {"lap_time": 138.0, "pit_on_lap": 27, "stop_s": 58,
                 "car_id": "ferrari296gt3",
                 "car_name": ["Ferrari 296 GT3", "Porsche 911 GT3 R",
                              "BMW M4 GT3"][i % 3],
                 "tank": 104.0, "burn": 3.55}
                for i in range(3)
            ]
            self.cars[1].update({"lap_time": 139.5, "pit_on_lap": 26, "stop_s": 61})
            self.cars[2].update({"lap_time": 140.2, "pit_on_lap": 28, "stop_s": 55})

    def ensure_connected(self):
        return True

    def sim_now(self) -> datetime:
        """Simulated wall clock. The engine times pit stalls in wall time,
        so the 60x-compressed demo must hand it a 60x clock -- otherwise a
        64 s stop lasts ~1 real second and classifies as a tow."""
        sim_t = (time.monotonic() - self.t0) * self.speedup
        return self._epoch + timedelta(seconds=sim_t)

    def frame(self) -> dict:
        sim_t = (time.monotonic() - self.t0) * self.speedup
        on_pit, surface, lap_arr, last = [], [], [], []
        for car in self.cars:
            lap = int(sim_t // car["lap_time"]) + 1
            pit_start = car["pit_on_lap"] * car["lap_time"]
            in_window = pit_start <= sim_t < pit_start + car["stop_s"] + 20
            in_stall = pit_start + 8 <= sim_t < pit_start + 8 + car["stop_s"]
            on_pit.append(in_window)
            surface.append(TRK_IN_PIT_STALL if in_stall else TRK_ON_TRACK)
            lap_arr.append(min(lap, car["pit_on_lap"]) if in_window else lap)
            last.append(car["lap_time"] + (hash(lap) % 10) / 10)
        dist = [(sim_t % c["lap_time"]) / c["lap_time"] for c in self.cars]
        # race order = laps completed + fraction of the current lap, so the
        # demo tower actually shuffles as the faster cars pull away.
        order = sorted(range(len(self.cars)), key=lambda i: -(lap_arr[i] + dist[i]))
        positions = [0] * len(self.cars)
        for rank, i in enumerate(order):
            positions[i] = rank + 1
        return {
            "CarIdxOnPitRoad": on_pit,
            "CarIdxTrackSurface": surface,
            "CarIdxLap": lap_arr,
            "CarIdxLastLapTime": last,
            "CarIdxLapDistPct": dist,
            "CarIdxPosition": positions,
            "CarIdxClassPosition": positions,  # demo field is single-class
            "SessionFlags": 0,
            "SessionTimeRemain": max(0, 3 * 3600 - sim_t),
            "SessionNum": 0,
            "FuelLevel": self._demo_fuel(sim_t),
            "PlayerCarIdx": self.player_idx,
            "IsOnTrack": True,
        }

    def _demo_fuel(self, sim_t: float) -> float:
        # burn continuously, refill when our car's demo stop completes -- so
        # the measured-fuel path sees a realistic gauge, refuel jump included
        car = self.cars[self.player_idx]
        pit_end = car["pit_on_lap"] * car["lap_time"] + 8 + car["stop_s"]
        since_fill = sim_t - pit_end if sim_t >= pit_end else sim_t
        return max(2.0, car["tank"] - (since_fill / car["lap_time"]) * car["burn"])

    def session_info(self) -> dict:
        return {
            "track_id": self.track_id,
            "track_display": self.track_display,
            "session_type": "Race",
            "player_car_idx": self.player_idx,
            "session_id": "demo",
            "drivers": [
                {"car_idx": i, "cust_id": 1000 + i,
                 "name": f"Demo {c['car_name'].split()[0]}",
                 "team_id": 9000 + i,
                 "car_id": c["car_id"], "car_number": str(i + 2),
                 "car_name": c["car_name"],
                 "class_id": "gt3", "is_pace_car": False}
                for i, c in enumerate(self.cars)
            ],
        }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

class ConsoleDisplay:
    """ANSI live table + scrolling event log. No dependencies."""

    # Where the fuel numbers for a row come from, in plain words.
    BASIS_LABELS = {
        "MEASURED": "our fuel gauge",
        "MULTI_OBSERVATION": "watched 2+ pit stops",
        "SINGLE_OBSERVATION": "watched 1 pit stop",
        "PRIOR_ONLY": "no pit stops yet",
    }

    PIT_STATE_LABELS = {
        "RACING": "on track",
        "ENTERING": "pit entry",
        "IN_STALL": "in pits",
        "EXITING": "pit exit",
    }

    # Pit stop classifications for the event log, in plain words.
    STOP_LABELS = {
        "FUEL_AND_TYRES": "fuel and tyres",
        "FUEL_ONLY": "fuel only, no tyres",
        "TYRES_ONLY": "tyres only, no fuel",
        "SPLASH": "small splash of fuel",
        "DRIVE_THROUGH": "drive-through penalty",
        "DAMAGE": "repair or tow",
    }

    def __init__(self, max_log: int = 8):
        self.log: List[str] = []
        self.max_log = max_log
        self.names: Dict[int, str] = {}
        self.numbers: Dict[int, str] = {}
        self.brands: Dict[int, str] = {}
        self.own_car: str = ""

    def set_roster(self, drivers: List[dict], player_car_idx: Optional[int] = None) -> None:
        for d in drivers:
            if d["car_idx"] is not None:
                self.names[d["car_idx"]] = d["name"][:20]
                self.numbers[d["car_idx"]] = d.get("car_number", "")
                car_name = d.get("car_name") or d.get("car_id") or ""
                # manufacturer only ("Porsche 911 GT3 R" -> "Porsche")
                self.brands[d["car_idx"]] = car_name.split()[0][:12] if car_name else ""
                if player_car_idx is not None and d["car_idx"] == player_car_idx:
                    self.own_car = car_name

    def add_event(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {msg}")
        self.log = self.log[-self.max_log:]

    def render(
        self,
        predictions: List[PitPrediction],
        engine: PitPredictionEngine,
        session_time_remain: Optional[float],
        connected: bool,
        uplink_status: Optional[str] = None,
        validation_line: str = "",
    ) -> None:
        lines = []
        status = "connected" if connected else "waiting for the sim..."
        remain = self._fmt_duration(session_time_remain)
        up = f"   |   relay: {uplink_status}" if uplink_status else ""
        our_car = f"   |   our car: {self.own_car}" if self.own_car else ""
        lines.append(
            f"  PIT PREDICTOR{our_car}   |   iRacing: {status}"
            f"   |   race time left: {remain}{up}"
        )
        if validation_line:
            lines.append(validation_line)
        lines.append("")
        header1 = (
            f"  {'Car':>4} {'Team/Driver':<20} {'Car':<12} {'Lap':>4} {'Where':<9} "
            f"{'Avg lap':>8} {'Best':>8} "
            f"{'Fuel left':>9} {'Next pit':>8} {'Pit lap':>9} {'Time to':>8} "
            f"{'Stops':>6} {'Conf':>5}  Fuel estimate"
        )
        header2 = (
            f"  {'#':>4} {'':<20} {'brand':<12} {'now':>4} {'':<9} "
            f"{'(last 5)':>8} {'lap':>8} "
            f"{'(laps)':>9} {'on lap':>8} {'window':>9} {'pit':>8} "
            f"{'left':>6} {'%':>5}  based on"
        )
        lines.append(header1)
        lines.append(header2)
        lines.append("  " + "-" * (len(header1) - 2))

        preds = sorted(predictions, key=lambda p: p.laps_of_fuel_remaining)
        for p in preds:
            state = engine.competitors.get(p.car_idx)
            raw_state = state.pit_state.value if state else "?"
            pit_state = self.PIT_STATE_LABELS.get(raw_state, raw_state)
            name = self.names.get(p.car_idx, f"Car {p.car_idx}")
            num = self.numbers.get(p.car_idx, "")
            brand = self.brands.get(p.car_idx, "")
            recent = state.lap_times[-5:] if state and state.lap_times else []
            avg_lap = self._fmt_lap(sum(recent) / len(recent) if recent else 0.0)
            best_lap = self._fmt_lap(state.best_lap_time_s if state else 0.0)
            window = f"{p.predicted_pit_lap_min}-{p.predicted_pit_lap_max}"
            pit_in = self._fmt_eta(p.predicted_pit_time)
            urgent = "->" if p.laps_of_fuel_remaining <= 3 else "  "
            stops = self._fmt_stops(p)
            basis = self.BASIS_LABELS.get(p.basis.value, p.basis.value)
            lines.append(
                f"{urgent}{num:>4} {name:<20} {brand:<12} "
                f"{state.current_lap if state else 0:>4} "
                f"{pit_state:<9} {avg_lap:>8} {best_lap:>8} "
                f"{p.laps_of_fuel_remaining:>9.1f} "
                f"{p.predicted_pit_lap:>8} {window:>9} {pit_in:>8} "
                f"{stops:>6} {p.confidence:>5.0%}  {basis}"
            )

        if not preds:
            lines.append("  (no cars tracked yet)")

        lines.append("")
        lines.append(
            "  How to read this: each car has enough fuel for 'Fuel left' more laps"
            " and must pit around 'Next pit on lap' (window = earliest to latest)."
        )
        lines.append(
            "  'Avg lap' = average of that car's last 5 racing laps"
            " (laps behind the safety car don't count).  'Best lap' = fastest of the session."
        )
        lines.append(
            "  'Stops left' = pit stops still needed to reach the finish"
            "  (s: last stop is only a splash of fuel,  *: saving fuel could skip it)."
        )
        lines.append(
            "  '->' at the line start = that car has to pit within 3 laps."
            "  'Conf %' = how much to trust the numbers on that line."
        )
        lines.append("")
        lines.append("  Recent events:")
        for entry in self.log or ["  (none)"]:
            lines.append(f"    {entry}")

        sys.stdout.write("\033[2J\033[H" + "\n".join(lines) + "\n")
        sys.stdout.flush()

    @staticmethod
    def _fmt_stops(p: PitPrediction) -> str:
        """'2', '1s' (final stop is a splash), '1*' (could save to skip), '0'."""
        if p.stops_remaining is None:
            return "-"
        if p.stops_remaining == 0:
            return "0"
        tag = ""
        if p.save_to_skip_l_per_lap:
            tag = "*"
        elif p.final_stop_fill_l is not None and p.final_stop_fill_l < 30:
            tag = "s"
        return f"{p.stops_remaining}{tag}"

    @staticmethod
    def _fmt_lap(seconds: Optional[float]) -> str:
        """Lap time as m:ss.t, or '-' when no lap has been recorded yet."""
        if not seconds or seconds <= 0:
            return "-"
        m, s = divmod(seconds, 60)
        return f"{int(m)}:{s:04.1f}"

    @staticmethod
    def _fmt_eta(ts: Optional[datetime]) -> str:
        if ts is None:
            return "no stop"
        delta = (ts - datetime.utcnow()).total_seconds()
        if delta <= 0:
            return "NOW"
        return f"{int(delta // 60)}m{int(delta % 60):02d}s"

    @staticmethod
    def _fmt_duration(seconds: Optional[float]) -> str:
        if seconds is None or seconds < 0 or seconds > 10 * 3600:
            return "--"
        h, rem = divmod(int(seconds), 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def describe_pit(evt: PitStopEvent, display: ConsoleDisplay) -> str:
    name = display.names.get(evt.car_idx, f"Car {evt.car_idx}")
    if evt.classified_as.value == "DRIVE_THROUGH":
        return f"{name}: drive-through penalty (no service)"
    kind = ConsoleDisplay.STOP_LABELS.get(
        evt.classified_as.value, evt.classified_as.value
    )
    return (
        f"{name}: pitted for {evt.stall_duration_s:.0f}s -- {kind}, "
        f"took ~{evt.inferred_fuel_added_l:.0f}L of fuel"
    )


def describe_stint(evt: StintEvent, display: ConsoleDisplay) -> str:
    name = display.names.get(evt.car_idx, f"Car {evt.car_idx}")
    return (
        f"{name}: finished stint {evt.stint_number} "
        f"(laps {evt.start_lap}-{evt.end_lap}, "
        f"{evt.green_laps} green / {evt.yellow_laps} yellow), "
        f"used {evt.inferred_burn_l_per_lap:.2f}L of fuel per lap"
    )



class GroundTruthValidator:
    """
    Practice/test-session mode: run the competitor-style inference on YOUR car
    (the one car with real FuelLevel) and score the model against reality.
    """

    def __init__(self):
        self.last_lap = -1
        self.last_fuel = None
        self.actual_burns: list = []
        self.line = ""

    def update(self, frame: dict, engine: "PitPredictionEngine") -> None:
        idx = frame.get("PlayerCarIdx")
        fuel = frame.get("FuelLevel")
        if idx is None or idx < 0 or fuel is None or fuel <= 0:
            return
        try:
            lap = frame["CarIdxLap"][idx]
        except (IndexError, TypeError):
            return
        if lap != self.last_lap:
            if self.last_fuel is not None and lap == self.last_lap + 1:
                burned = self.last_fuel - fuel
                if 0 < burned < 10:
                    self.actual_burns.append(burned)
                    self.actual_burns = self.actual_burns[-5:]
            self.last_lap = lap
            self.last_fuel = fuel

        model_fuel = engine.estimated_fuel_l(idx)
        if model_fuel is None:
            return
        diff = model_fuel - fuel
        if abs(diff) < 0.3:
            verdict = "spot on"
        else:
            verdict = f"{abs(diff):.1f}L too {'high' if diff > 0 else 'low'}"
        parts = [
            f"it estimated {model_fuel:.1f}L in our tank, the gauge shows {fuel:.1f}L"
            f" -> {verdict}"
        ]
        if self.actual_burns:
            actual_burn = sum(self.actual_burns) / len(self.actual_burns)
            state = engine.competitors.get(idx)
            ref = engine._reference_for(state) if state else None
            if state and ref:
                pace = state.rolling_pace_s or ref.baseline_lap_time_s
                model_burn = reference_burn_for_pace(
                    ref, pace, state.pace_baseline_s
                ) * state.personal_burn_factor
                parts.append(
                    f"estimated {model_burn:.2f}L used per lap, really {actual_burn:.2f}L"
                )
        self.line = (
            "  SELF-TEST: rivals' fuel is invisible, so it is estimated from laps"
            " driven, pace and pit stop lengths. The same estimate, done for our"
            " car and\n             checked against the real gauge: "
            + "  |  ".join(parts)
            + "   (small error = the table below can be trusted)"
        )


def _persist_id(d: dict) -> int:
    """Stable identity for calibration persistence. In team sessions the
    roster's UserID changes at every driver swap, so prefer TeamID."""
    return d.get("team_id") or d.get("cust_id", -1)


def _save_snapshot(path: str, engine: PitPredictionEngine, subsession_id: str) -> dict:
    payload = {
        "type": "state",
        "subsession_id": subsession_id,
        "saved_at": datetime.utcnow().isoformat(),
        "competitors": engine.export_state(),
    }
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)  # atomic -- a crash mid-write never corrupts the file
    return payload


def _fetch_relay_state(server: str, token: str) -> Optional[dict]:
    """Pull engine recovery state parked on the relay (client-PC handoff)."""
    import urllib.request
    try:
        req = urllib.request.Request(
            server.rstrip("/") + "/api/client-state",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
            return data if data.get("competitors") else None
    except Exception:
        return None


def _snapshot_age_s(saved: dict) -> float:
    try:
        return (datetime.utcnow() - datetime.fromisoformat(saved["saved_at"])).total_seconds()
    except (KeyError, ValueError):
        return float("inf")


def build_snapshot(
    engine: PitPredictionEngine,
    display: ConsoleDisplay,
    preds: List[PitPrediction],
    session_meta: dict,
    subsession_id: str,
    time_remaining_s: Optional[float],
) -> dict:
    own_idx = session_meta.get("player_car_idx")
    compare = {}
    if own_idx is not None and own_idx >= 0:
        try:
            compare = engine.compare_to_field(own_idx, time_remaining_s)
        except Exception:
            compare = {}
    rows = []
    for p in preds:
        s = engine.competitors.get(p.car_idx)
        pace = s.rolling_pace_s if s else 0
        rows.append({
            **p.to_dict(),
            "predicted_pit_time": None,  # ETA in minutes travels better than ts
            "pit_eta_min": round(p.laps_of_fuel_remaining * pace / 60, 2)
                           if pace and p.predicted_pit_time else None,
            "name": display.names.get(p.car_idx, f"Car {p.car_idx}"),
            "car_number": display.numbers.get(p.car_idx, ""),
            "car_id": s.car_id if s else "",
            "position": s.position if s else 0,
            "class_position": s.class_position if s else 0,
            "current_lap": s.current_lap if s else 0,
            "pit_state": s.pit_state.value if s else "RACING",
            "last_pit_lap": s.last_pit_lap if s else 0,
            "anchor_uncertain": s.anchor_uncertain if s else False,
            "last_calibrated_at": None,
            "vs_us": compare.get(p.car_idx),
        })
    return {
        "type": "snapshot",
        "session": {
            "track_id": session_meta.get("track_id", ""),
            "track_display": session_meta.get("track_display", "") or session_meta.get("track_id", ""),
            "session_type": session_meta.get("session_type", ""),
            "player_car_idx": session_meta.get("player_car_idx"),
            "car_count": len(rows),
            "subsession_id": subsession_id,
            "time_remaining_s": time_remaining_s,
        },
        "predictions": rows,
        "ts": datetime.utcnow().isoformat() + "Z",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Live iRacing pit predictor")
    ap.add_argument("--refs", help="Path to references.json")
    ap.add_argument("--demo", action="store_true", help="Run with simulated data")
    ap.add_argument("--state", help="Path to persist/restore driver burn factors (JSON)")
    ap.add_argument("--server", help="Relay base URL, e.g. https://pitwall.example.com")
    ap.add_argument("--token", help="INGEST_TOKEN for the relay")
    args = ap.parse_args()

    display = ConsoleDisplay()
    validator = GroundTruthValidator()
    uplink = Uplink(args.server, args.token) if args.server and args.token else None

    def warn(msg: str) -> None:
        """Config problems go to the team dashboard too -- an engineer reading
        predictions deserves to know they are running on generic numbers."""
        display.add_event(msg)
        if uplink:
            uplink.send_event(msg)

    refs, ref_gaps = load_references(args.refs, warn)
    # --demo with --refs simulates the field described by the reference file
    # (real car ids, tanks, burns); without refs it falls back to the
    # built-in 3-car sample.
    source = DemoSource(refs if args.refs else None) if args.demo else IRacingSource()

    engine: Optional[PitPredictionEngine] = None
    latest_info: dict = {}
    session_id_seen: Optional[str] = None
    last_render = 0.0
    last_roster_refresh = 0.0
    last_on_track = -1.0  # monotonic ts our member was last in the car

    try:
        while True:
            loop_start = time.monotonic()
            frame = source.frame()

            if frame is None:
                display.render([], engine or PitPredictionEngine(), None, connected=False)
                time.sleep(2)
                continue

            # active-driver flag for relay arbitration between team PCs, with
            # hold-down so a tow or brief reset doesn't flap the feed
            if frame.get("IsOnTrack"):
                last_on_track = loop_start
            if uplink:
                uplink.driver_active = (
                    last_on_track >= 0
                    and loop_start - last_on_track < DRIVER_ACTIVE_HOLD_S
                )

            # (re)initialise engine when a new session appears
            info = {}
            if engine is None or loop_start - last_roster_refresh > 30:
                info = source.session_info()
                last_roster_refresh = loop_start

            session_num = frame.get("SessionNum")
            if engine is not None and session_num is not None:
                if getattr(engine, "_session_num", None) not in (None, session_num):
                    # server rotated practice -> qualy -> race: fresh stints,
                    # keep what we learned about each driver
                    factors = engine.export_state()
                    engine = None
                    _carry = factors
                    display.add_event(f"session rotated (now #{session_num})")
                else:
                    engine._session_num = session_num

            if engine is None and not info:
                # practice/test session where the YAML is missing or partial:
                # never block predictions on it -- start with defaults and let
                # a later roster refresh fill in names and references
                info = source.session_info() or {"track_id": "unknown", "session_id": "", "drivers": []}

            if info or engine is None:
                sid = (info or {}).get("session_id", "") or "local"
                if engine is None or (sid and sid != session_id_seen):
                    engine = PitPredictionEngine(
                        reference_provider=make_reference_provider(
                            refs,
                            (info or {}).get("track_id", "unknown"),
                            ref_gaps,
                            warn,
                        ),
                        session_id=sid or "live",
                    )
                    def _pit_cb(e):
                        msg = describe_pit(e, display)
                        display.add_event(msg)
                        if uplink:
                            uplink.send_event(msg)

                    def _stint_cb(e):
                        msg = describe_stint(e, display)
                        display.add_event(msg)
                        if uplink:
                            uplink.send_event(msg)

                    engine.on_pit_stop(_pit_cb)
                    engine.on_stint(_stint_cb)
                    saved = None
                    saved_from = "local file"
                    if args.state and Path(args.state).exists():
                        saved = json.loads(Path(args.state).read_text())
                    local_matches = bool(
                        saved and saved.get("subsession_id") == sid
                        and _snapshot_age_s(saved) < 6 * 3600
                    )
                    if not local_matches and args.server and args.token:
                        relay_saved = _fetch_relay_state(args.server, args.token)
                        if (relay_saved and relay_saved.get("subsession_id") == sid
                                and _snapshot_age_s(relay_saved) < 6 * 3600):
                            saved = relay_saved
                            saved_from = "relay (handoff from another PC)"
                    if saved:
                        # register first so import can match cust_ids
                        for d in (info or {}).get("drivers", []):
                            if not d["is_pace_car"] and d["car_idx"] is not None:
                                engine.register_competitor(
                                    d["car_idx"], _persist_id(d), d["car_id"], d["class_id"]
                                )
                        same = (
                            saved.get("subsession_id") == sid
                            and _snapshot_age_s(saved) < 6 * 3600
                        )
                        engine.import_state(
                            saved.get("competitors", saved), same_session=same
                        )
                        display.add_event(
                            f"restored mid-race state via {saved_from}"
                            if same
                            else "warm-started burn factors from previous race"
                        )
                    engine._session_num = session_num
                    if "_carry" in dir() and _carry:
                        engine.import_state(_carry, same_session=False)
                        _carry = None
                    session_id_seen = sid
                    display.add_event(f"session initialised ({(info or {}).get('track_id', 'unknown')})")
                    # BoP sanity check: compare live effective tank vs reference
                    eff = (info or {}).get("effective_tank_l")
                    pcar = (info or {}).get("player_car_id", "")
                    if eff and pcar:
                        row = refs.get(f"{pcar}|{(info or {}).get('track_id')}")
                        if row and abs(row.tank_capacity_l - eff) > 2.0:
                            warn(
                                f"WARNING: reference tank for {pcar} is "
                                f"{row.tank_capacity_l:.0f}L but BoP-effective tank is "
                                f"{eff:.0f}L -- update references.json (predictions "
                                f"for this car model are skewed until fixed)"
                            )
                # keep roster fresh (driver swaps in team events)
                for d in (info or {}).get("drivers", []):
                    if not d["is_pace_car"] and d["car_idx"] is not None:
                        engine.register_competitor(
                            d["car_idx"], _persist_id(d), d["car_id"], d["class_id"]
                        )
                display.set_roster(
                    (info or {}).get("drivers", []),
                    player_car_idx=(info or {}).get("player_car_idx"),
                )
                latest_info = info or latest_info

            if engine:
                # demo compresses time 60x -- the engine must run on the
                # simulated clock or stall durations collapse to ~1 s
                sim_now = source.sim_now() if hasattr(source, "sim_now") else None
                engine.process_frame(frame, sim_now)
                validator.update(frame, engine)

                if loop_start - last_render >= DISPLAY_EVERY_S:
                    remain = frame.get("SessionTimeRemain")
                    preds = engine.predict_all(session_time_remaining_s=remain,
                                               now=sim_now)
                    display.render(preds, engine, remain, connected=True,
                                   uplink_status=uplink.status if uplink else None,
                                   validation_line=validator.line)
                    last_render = loop_start
                    if uplink:
                        uplink.send_snapshot(build_snapshot(
                            engine, display, preds,
                            latest_info,
                            session_id_seen or "",
                            remain,
                        ))
                    if args.state:
                        payload = _save_snapshot(args.state, engine, session_id_seen or "")
                        if uplink:
                            uplink._put(payload)  # park recovery state on relay

            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.0, 1.0 / POLL_HZ - elapsed))

    except KeyboardInterrupt:
        if engine and args.state:
            _save_snapshot(args.state, engine, session_id_seen or "")
            print(f"\nBurn factors saved to {args.state}")
        print("Stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
