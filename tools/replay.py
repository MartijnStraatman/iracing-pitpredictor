#!/usr/bin/env python3
"""
Replay a relay recording and score what the engine predicted against what the
cars actually did.

The relay appends every accepted payload to events.jsonl verbatim, so a race
is fully recoverable after the fact: each snapshot carries every car's lap,
pit state, stint anchor and prediction. That is enough to answer the questions
a bad race raises without re-running telemetry.

    python tools/replay.py relay/data/events.jsonl
    python tools/replay.py events.jsonl --car 6          # one car in detail
    python tools/replay.py events.jsonl --stops          # every observed stop

Reports:
  * stops the engine recorded before the green flag -- a car "pitting" on
    lap 0/1 is the grid, not a stop, and its fabricated fill poisons the
    stint anchor for the rest of the race;
  * for every observed stop, what was predicted five laps earlier and whether
    the stop fell inside the published window;
  * anchor churn: cars whose stint anchor moved without a stop, which is
    missed-pit inference re-anchoring on a model that had drifted.

Stdlib only, like everything else the client ships.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional

BACKTEST_LEAD_LAPS = 5   # how far ahead of a stop we score the prediction


class Sample:
    """One car's row in one snapshot."""

    __slots__ = ("ts", "lap", "last_pit_lap", "pit_state", "laps_of_fuel",
                 "pred_lap", "pred_min", "pred_max", "basis", "confidence",
                 "stops_remaining", "anchor_uncertain")

    def __init__(self, ts: str, row: dict):
        self.ts = ts
        self.lap = row.get("current_lap", 0)
        self.last_pit_lap = row.get("last_pit_lap", 0)
        self.pit_state = row.get("pit_state", "RACING")
        self.laps_of_fuel = row.get("laps_of_fuel_remaining")
        self.pred_lap = row.get("predicted_pit_lap")
        self.pred_min = row.get("predicted_pit_lap_min")
        self.pred_max = row.get("predicted_pit_lap_max")
        self.basis = row.get("basis", "")
        self.confidence = row.get("confidence")
        self.stops_remaining = row.get("stops_remaining")
        self.anchor_uncertain = row.get("anchor_uncertain", False)


class Car:
    def __init__(self, key: str):
        self.key = key
        self.name = ""
        self.number = ""
        self.car_id = ""
        self.samples: List[Sample] = []

    @property
    def label(self) -> str:
        num = f"#{self.number} " if self.number else ""
        return f"{num}{self.name or self.key}"

    def stops(self) -> List[dict]:
        """Observed stops, taken from the stint anchor moving forward."""
        out = []
        prev: Optional[Sample] = None
        for s in self.samples:
            if prev is not None and s.last_pit_lap != prev.last_pit_lap:
                out.append({
                    "lap": s.last_pit_lap,
                    "ts": s.ts,
                    "at_lap": s.lap,
                    "from": prev.last_pit_lap,
                    # a stop the car drove into leaves a pit state behind it;
                    # an anchor that moves with the car on track is inference
                    "observed": prev.pit_state in ("IN_STALL", "EXITING", "ENTERING"),
                })
            prev = s
        return out

    def prediction_at_lap(self, lap: int) -> Optional[Sample]:
        for s in self.samples:
            if s.lap >= lap and s.pred_lap is not None:
                return s
        return None


def load(path: Path) -> Iterator[dict]:
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def build(path: Path):
    cars: Dict[str, Car] = {}
    messages: List[dict] = []
    session: dict = {}
    snapshots = 0

    for payload in load(path):
        kind = payload.get("type")
        if kind == "event":
            messages.append(payload)
            continue
        if kind != "snapshot":
            continue
        snapshots += 1
        session = payload.get("session", session)
        ts = payload.get("ts", "")
        for row in payload.get("predictions", []):
            key = str(row.get("cust_id") or row.get("car_idx"))
            car = cars.get(key)
            if car is None:
                car = cars[key] = Car(key)
            car.name = row.get("name") or car.name
            car.number = row.get("car_number") or car.number
            car.car_id = row.get("car_id") or car.car_id
            car.samples.append(Sample(ts, row))

    return session, cars, messages, snapshots


# -- reports ----------------------------------------------------------------

def report_pre_green(cars: Dict[str, Car]) -> None:
    print("\n== stops recorded on lap 0 or 1 ==")
    hits = []
    for car in cars.values():
        for stop in car.stops():
            if stop["lap"] <= 1:
                hits.append((car, stop))
    if not hits:
        print("  none -- no car was anchored to a stop before it had raced a lap")
        return
    print(f"  {len(hits)} car(s) anchored to a stop before racing a lap.")
    print("  A car sitting in its box waiting for the green is not pitting;")
    print("  the wait read as service time fabricates a fuel load per car.")
    for car, stop in hits:
        print(f"    {car.label:28s} anchor -> lap {stop['lap']} "
              f"(seen at lap {stop['at_lap']}, {stop['ts']})")


def report_stops(cars: Dict[str, Car], only: Optional[str]) -> None:
    print("\n== observed stops ==")
    for car in sorted(cars.values(), key=lambda c: c.label):
        if only and only not in (car.number, car.name, car.key):
            continue
        stops = car.stops()
        if not stops:
            continue
        laps = ", ".join(
            f"L{s['lap']}" + ("" if s["observed"] else " (inferred)")
            for s in stops
        )
        print(f"  {car.label:28s} {laps}")


def report_backtest(cars: Dict[str, Car]) -> None:
    print(f"\n== predicted vs actual pit lap ({BACKTEST_LEAD_LAPS} laps ahead) ==")
    errors: List[int] = []
    inside = 0
    scored = 0
    rows = []
    for car in sorted(cars.values(), key=lambda c: c.label):
        for stop in car.stops():
            actual = stop["lap"]
            if actual <= 1 or not stop["observed"]:
                continue
            sample = car.prediction_at_lap(actual - BACKTEST_LEAD_LAPS)
            if sample is None or sample.pred_lap is None:
                continue
            scored += 1
            err = sample.pred_lap - actual
            errors.append(err)
            band = ""
            if sample.pred_min is not None and sample.pred_max is not None:
                if sample.pred_min <= actual <= sample.pred_max:
                    inside += 1
                    band = "in band"
                else:
                    band = "OUTSIDE"
            rows.append(
                f"  {car.label:24s} predicted L{sample.pred_lap:<4d} "
                f"actual L{actual:<4d} err {err:+3d}  "
                f"[{sample.pred_min}-{sample.pred_max}] {band:8s} "
                f"{sample.basis}"
            )
    if not scored:
        print("  no observed stop had a prediction five laps earlier to score")
        return
    for line in rows:
        print(line)
    print(f"\n  scored {scored} stop(s): "
          f"median error {statistics.median(errors):+.1f} laps, "
          f"mean |error| {statistics.fmean(abs(e) for e in errors):.2f}, "
          f"{inside}/{scored} inside the published window")
    late = sum(1 for e in errors if e > 0)
    print(f"  predicted too LATE (car would run dry): {late}/{scored}")


def report_anchor_churn(cars: Dict[str, Car]) -> None:
    print("\n== anchors moved without an observed stop ==")
    any_hit = False
    for car in sorted(cars.values(), key=lambda c: c.label):
        churn = [s for s in car.stops() if not s["observed"]]
        if churn:
            any_hit = True
            print(f"  {car.label:28s} "
                  + ", ".join(f"L{s['from']}->L{s['lap']}" for s in churn))
    if not any_hit:
        print("  none -- every anchor move followed a stop the engine watched")


def report_uncertainty(cars: Dict[str, Car]) -> None:
    flagged = [c for c in cars.values()
               if any(s.anchor_uncertain for s in c.samples)]
    if flagged:
        print(f"\n== anchor_uncertain raised for {len(flagged)} car(s) ==")
        for car in sorted(flagged, key=lambda c: c.label)[:20]:
            n = sum(1 for s in car.samples if s.anchor_uncertain)
            print(f"  {car.label:28s} {n} snapshot(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", help="relay events.jsonl")
    ap.add_argument("--car", help="filter the stop list to one car number/name")
    ap.add_argument("--stops", action="store_true", help="list every observed stop")
    args = ap.parse_args()

    path = Path(args.recording)
    if not path.exists():
        print(f"no such recording: {path}")
        return 1

    session, cars, messages, snapshots = build(path)
    if not snapshots:
        print(f"{path} holds no snapshots -- nothing to replay.")
        return 1

    print(f"== {path} ==")
    print(f"  track        {session.get('track_display') or session.get('track_id')}")
    print(f"  subsession   {session.get('subsession_id')}")
    print(f"  snapshots    {snapshots}")
    print(f"  cars         {len(cars)}")
    print(f"  log messages {len(messages)}")

    report_pre_green(cars)
    report_anchor_churn(cars)
    report_backtest(cars)
    report_uncertainty(cars)
    if args.stops or args.car:
        report_stops(cars, args.car)

    warn = [m for m in messages if "WARNING" in str(m.get("message", ""))]
    if warn:
        print(f"\n== {len(warn)} warning(s) logged during the session ==")
        seen = set()
        for m in warn:
            msg = str(m.get("message"))
            if msg not in seen:
                seen.add(msg)
                print(f"  {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
