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
    "track_id": "spa",
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
Rows with track_id "*" act as car-level fallbacks for unknown tracks.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

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

def load_references(path: Optional[str]) -> Dict[str, CarTrackReference]:
    """
    Load reference rows keyed by 'car_id|track_id'.

    Track-specific rows inherit any missing fields from the same car's
    wildcard row (track_id "*"). Put car-level properties -- tank_capacity_l,
    refuel_rate_l_per_s, tyre_change_time_s -- in the wildcard row once, and
    keep track rows down to the track-dependent numbers (burn rates, lap time).
    """
    refs: Dict[str, CarTrackReference] = {}
    if not path:
        return refs
    data = json.loads(Path(path).read_text())
    valid = {f.name for f in CarTrackReference.__dataclass_fields__.values()}
    rows = [{k: v for k, v in row.items() if k in valid} for row in data]

    wildcards = {r["car_id"]: r for r in rows if r.get("track_id", "*") == "*"}
    for row in rows:
        base = wildcards.get(row["car_id"], {})
        merged = {**base, **row}
        ref = CarTrackReference(**merged)
        refs[f"{ref.car_id}|{ref.track_id}"] = ref
    return refs


def make_reference_provider(refs: Dict[str, CarTrackReference], track_id: str):
    def provider(state: CompetitorState) -> Optional[CarTrackReference]:
        return (
            refs.get(f"{state.car_id}|{track_id}")
            or refs.get(f"{state.car_id}|*")
            or None
        )
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
                        "car_number": d.get("CarNumber", ""),
                        "class_id": str(d.get("CarClassID", "")),
                        "is_pace_car": d.get("CarIsPaceCar", 0) == 1,
                    }
                    for d in drivers
                ],
            }
        except Exception:
            return {}


class DemoSource:
    """Simulated 3-car session so the display can be tested without iRacing."""

    def __init__(self):
        self.t0 = time.monotonic()
        self.cars = [
            {"lap_time": 138.0, "pit_on_lap": 27, "stop_s": 58},
            {"lap_time": 139.5, "pit_on_lap": 26, "stop_s": 61},
            {"lap_time": 140.2, "pit_on_lap": 28, "stop_s": 55},
        ]
        self.speedup = 60  # 1 real second = 1 simulated minute

    def ensure_connected(self):
        return True

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
            "FuelLevel": max(2.0, 104 - (sim_t / self.cars[0]["lap_time"]) * 3.55),
            "PlayerCarIdx": 0,
        }

    def session_info(self) -> dict:
        return {
            "track_id": "demo_spa",
            "track_display": "Circuit de Spa-Francorchamps (Demo)",
            "session_type": "Race",
            "player_car_idx": 0,
            "session_id": "demo",
            "drivers": [
                {"car_idx": i, "cust_id": 1000 + i, "name": f"Demo Team {i + 1}",
                 "team_id": 9000 + i,
                 "car_id": "ferrari296gt3", "car_number": str(11 * (i + 1)),
                 "class_id": "gt3", "is_pace_car": False}
                for i in range(len(self.cars))
            ],
        }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

class ConsoleDisplay:
    """ANSI live table + scrolling event log. No dependencies."""

    def __init__(self, max_log: int = 8):
        self.log: List[str] = []
        self.max_log = max_log
        self.names: Dict[int, str] = {}
        self.numbers: Dict[int, str] = {}

    def set_roster(self, drivers: List[dict]) -> None:
        for d in drivers:
            if d["car_idx"] is not None:
                self.names[d["car_idx"]] = d["name"][:22]
                self.numbers[d["car_idx"]] = d.get("car_number", "")

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
        status = "CONNECTED" if connected else "WAITING FOR IRACING..."
        remain = self._fmt_duration(session_time_remain)
        up = f"   |   relay: {uplink_status}" if uplink_status else ""
        lines.append(f"  PIT PREDICTOR   |   {status}   |   session remaining: {remain}{up}")
        if validation_line:
            lines.append(validation_line)
        lines.append("")
        header = (
            f"  {'#':>4} {'Team/Driver':<22} {'Lap':>4} {'State':<9} "
            f"{'Fuel laps':>9} {'Pit lap':>8} {'Window':>9} {'Pit in':>8} "
            f"{'Stops':>6} {'Conf':>5}  Basis"
        )
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))

        preds = sorted(predictions, key=lambda p: p.laps_of_fuel_remaining)
        for p in preds:
            state = engine.competitors.get(p.car_idx)
            pit_state = state.pit_state.value if state else "?"
            name = self.names.get(p.car_idx, f"Car {p.car_idx}")
            num = self.numbers.get(p.car_idx, "")
            window = f"{p.predicted_pit_lap_min}-{p.predicted_pit_lap_max}"
            pit_in = self._fmt_eta(p.predicted_pit_time)
            urgent = "->" if p.laps_of_fuel_remaining <= 3 else "  "
            stops = self._fmt_stops(p)
            lines.append(
                f"{urgent}{num:>4} {name:<22} {state.current_lap if state else 0:>4} "
                f"{pit_state:<9} {p.laps_of_fuel_remaining:>9.1f} "
                f"{p.predicted_pit_lap:>8} {window:>9} {pit_in:>8} "
                f"{stops:>6} {p.confidence:>5.0%}  {p.basis.value}"
            )

        if not preds:
            lines.append("  (no cars tracked yet)")

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
        return f"{name}: drive-through penalty"
    return (
        f"{name}: pit stop {evt.stall_duration_s:.0f}s in stall, "
        f"~{evt.inferred_fuel_added_l:.0f}L"
        f"{' + tyres' if evt.inferred_tyre_change else ''} "
        f"({evt.classified_as.value})"
    )


def describe_stint(evt: StintEvent, display: ConsoleDisplay) -> str:
    name = display.names.get(evt.car_idx, f"Car {evt.car_idx}")
    return (
        f"{name}: stint {evt.stint_number} ended, laps {evt.start_lap}-{evt.end_lap} "
        f"({evt.green_laps} green / {evt.yellow_laps} yellow), "
        f"burn {evt.inferred_burn_l_per_lap:.2f} L/lap"
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
        parts = [f"model {model_fuel:5.1f}L vs actual {fuel:5.1f}L (d{model_fuel - fuel:+5.1f}L)"]
        if self.actual_burns:
            actual_burn = sum(self.actual_burns) / len(self.actual_burns)
            state = engine.competitors.get(idx)
            ref = engine._reference_for(state) if state else None
            if state and ref:
                pace = state.rolling_avg_lap_time_s or ref.baseline_lap_time_s
                model_burn = reference_burn_for_pace(ref, pace) * state.personal_burn_factor
                parts.append(f"burn {model_burn:.2f} vs {actual_burn:.2f} L/lap (d{model_burn - actual_burn:+.2f})")
        self.line = "  OWN-CAR CHECK: " + "  |  ".join(parts)


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
        pace = s.rolling_avg_lap_time_s if s else 0
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

    source = DemoSource() if args.demo else IRacingSource()
    refs = load_references(args.refs)
    display = ConsoleDisplay()
    validator = GroundTruthValidator()
    uplink = Uplink(args.server, args.token) if args.server and args.token else None

    engine: Optional[PitPredictionEngine] = None
    latest_info: dict = {}
    session_id_seen: Optional[str] = None
    last_render = 0.0
    last_roster_refresh = 0.0

    try:
        while True:
            loop_start = time.monotonic()
            frame = source.frame()

            if frame is None:
                display.render([], engine or PitPredictionEngine(), None, connected=False)
                time.sleep(2)
                continue

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
                            refs, (info or {}).get("track_id", "unknown")
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
                        row = refs.get(f"{pcar}|{(info or {}).get('track_id')}") or refs.get(f"{pcar}|*")
                        if row and abs(row.tank_capacity_l - eff) > 2.0:
                            display.add_event(
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
                display.set_roster((info or {}).get("drivers", []))
                latest_info = info or latest_info

            if engine:
                engine.process_frame(frame)
                validator.update(frame, engine)

                if loop_start - last_render >= DISPLAY_EVERY_S:
                    remain = frame.get("SessionTimeRemain")
                    preds = engine.predict_all(session_time_remaining_s=remain)
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
