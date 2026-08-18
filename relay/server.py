"""
Pit Predictor relay server.

Receives prediction snapshots from the iRacing PC and fans them out to
team dashboards over Server-Sent Events.

Endpoints:
    POST /api/ingest      -- snapshot/event upload (Bearer INGEST_TOKEN)
    GET  /api/stream      -- SSE stream for dashboards (?key=VIEW_TOKEN if set)
    GET  /api/state       -- latest snapshot as JSON (same viewer auth)
    GET  /                -- dashboard
    GET  /health          -- liveness probe

Environment:
    INGEST_TOKEN  required -- shared secret the runner sends
    VIEW_TOKEN    optional -- if set, dashboard/stream require ?key=<token>
    EVENT_LOG     optional -- path to append-only JSONL of everything ingested
                              (default /data/events.jsonl; set empty to disable)
    DEMO          optional -- "1" starts a self-generating sample race so the
                              dashboard is live with no iRacing client
                              connected. For checking a deployment (URL,
                              HTTPS, phones) only: the feed is arithmetic in
                              this file, NOT the prediction engine, so it
                              validates nothing about prediction quality.
                              Leave unset in production; a real client's
                              snapshots would be overwritten every second.
"""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
VIEW_TOKEN = os.environ.get("VIEW_TOKEN", "")
EVENT_LOG = os.environ.get("EVENT_LOG", "/data/events.jsonl")
DEMO = os.environ.get("DEMO", "").lower() in ("1", "true", "yes")
MAX_EVENTS = 60

app = FastAPI(title="Pit Predictor Relay", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"


class Hub:
    """Latest state + fanout to connected SSE clients."""

    def __init__(self) -> None:
        self.snapshot: Optional[dict] = None
        self.client_state: Optional[dict] = None
        self.events: list = []
        self.last_ingest_ts: float = 0.0
        self.subscribers: Set[asyncio.Queue] = set()

    def ingest(self, payload: dict) -> None:
        self.last_ingest_ts = time.time()
        kind = payload.get("type")
        if kind == "state":
            # engine recovery state parked here for client-PC handoff;
            # viewers never see it, and it would bloat the event log
            self.client_state = payload
            return
        if kind == "snapshot":
            self.snapshot = payload
        elif kind == "event":
            self.events.append(payload)
            self.events = self.events[-MAX_EVENTS:]
        self._append_log(payload)
        self._broadcast(payload)

    def _broadcast(self, payload: dict) -> None:
        dead = []
        for q in self.subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.subscribers.discard(q)

    def _append_log(self, payload: dict) -> None:
        if not EVENT_LOG:
            return
        try:
            path = Path(EVENT_LOG)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps(payload) + "\n")
        except OSError:
            pass  # logging must never take the live feed down

    def feed_age_s(self) -> Optional[float]:
        return time.time() - self.last_ingest_ts if self.last_ingest_ts else None


hub = Hub()


def _check_viewer(request: Request) -> None:
    if VIEW_TOKEN and request.query_params.get("key") != VIEW_TOKEN:
        raise HTTPException(status_code=401, detail="Missing or wrong key")


@app.post("/api/ingest")
async def ingest(request: Request):
    auth = request.headers.get("authorization", "")
    if not INGEST_TOKEN or auth != f"Bearer {INGEST_TOKEN}":
        raise HTTPException(status_code=401, detail="Bad ingest token")
    payload = await request.json()
    # accept a single object or a batch list
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        hub.ingest(item)
    return {"ok": True, "received": len(items)}


@app.get("/api/state")
async def state(request: Request):
    _check_viewer(request)
    return JSONResponse(
        {
            "snapshot": hub.snapshot,
            "events": hub.events,
            "feed_age_s": hub.feed_age_s(),
        }
    )


@app.get("/api/stream")
async def stream(request: Request):
    _check_viewer(request)
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    hub.subscribers.add(queue)

    async def gen():
        try:
            # replay current state so a fresh client renders instantly
            if hub.snapshot:
                yield f"data: {json.dumps(hub.snapshot)}\n\n"
            for evt in hub.events[-10:]:
                yield f"data: {json.dumps(evt)}\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(item)}\n\n"
                except asyncio.TimeoutError:
                    age = hub.feed_age_s()
                    yield f"data: {json.dumps({'type': 'heartbeat', 'feed_age_s': age})}\n\n"
        finally:
            hub.subscribers.discard(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/client-state")
async def client_state(request: Request):
    """Engine recovery state for handing the client to another team PC.
    Requires the ingest token -- this is operational data, not viewer data."""
    auth = request.headers.get("authorization", "")
    if not INGEST_TOKEN or auth != f"Bearer {INGEST_TOKEN}":
        raise HTTPException(status_code=401, detail="Bad ingest token")
    return JSONResponse(hub.client_state or {})


@app.get("/health")
async def health():
    return {"ok": True, "feed_age_s": hub.feed_age_s(), "viewers": len(hub.subscribers)}


@app.get("/")
async def index(request: Request):
    _check_viewer(request)
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# Demo feed -- self-generated race, for testing a deployment without a client
# ---------------------------------------------------------------------------

DEMO_FIELD = [
    # idx, number, team, car, pace, stint_laps, start_offset_laps
    # Paces are deliberately close (a real GT3 field is): gaps stay in
    # seconds, so strategy -- not raw pace -- decides the order.
    (11, "24", "Kessel Racing", "ferrari296gt3", 139.95, 26, 0.14),
    (5, "63", "Iron Lynx", "lamborghinievogt3", 140.05, 28, 0.06),
    (3, "88", "Apex Endurance", "ferrari296gt3", 140.00, 29, 0.00),
    (9, "47", "AF Corse", "ferrari296gt3", 140.10, 29, -0.05),
    (2, "31", "Team WRT", "bmwm4gt3", 140.15, 24, -0.11),
    (7, "54", "Dinamic GT", "porsche992rgt3", 140.08, 31, -0.19),
    (14, "70", "Inception Racing", "mclaren720sgt3", 140.20, 33, -0.28),
]
DEMO_RACE_S = 3 * 3600
OUR_IDX = 3


async def _demo_feed() -> None:
    """Simulate a 3h race at 30x, pushing snapshots through the same Hub."""
    started = time.time()
    WARM_START_S = 105 * 60  # begin ~1h45 in: everyone calibrated, stops in play
    while True:
        sim_t = (WARM_START_S + (time.time() - started) * 30) % DEMO_RACE_S
        remaining = DEMO_RACE_S - sim_t
        rows = []
        for idx, num, name, car_id, pace, stint, offset in DEMO_FIELD:
            lap = int(sim_t / pace + offset)
            if lap < 1:
                continue
            stints_done = max(0, (lap - 1) // stint)
            last_pit = stints_done * stint if stints_done else 0
            laps_in = lap - last_pit
            fuel_laps = max(0.0, stint - laps_in + (0.5 - (sim_t % pace) / pace))
            conf = min(1.0, stints_done / 3)
            band = max(1, round((1.4 - conf) * 2))
            laps_left = remaining / pace + 1
            burn = 104 / stint
            shortfall = laps_left * burn + 0.5 - fuel_laps * burn
            stops = max(0, -(-shortfall // 104)) if shortfall > 0 else 0
            fill = (shortfall - (stops - 1) * 104) if stops else None
            save = round(fill / laps_left, 2) if stops and fill and fill / laps_left <= 0.3 else None
            in_pit = fuel_laps < 0.35
            rows.append({
                "car_idx": idx, "car_number": num, "name": name, "car_id": car_id,
                "current_lap": lap, "last_pit_lap": last_pit,
                "pit_state": "IN_STALL" if in_pit else "RACING",
                "laps_of_fuel_remaining": round(fuel_laps, 1),
                "predicted_pit_lap": lap + int(fuel_laps),
                "predicted_pit_lap_min": lap + int(fuel_laps) - band,
                "predicted_pit_lap_max": lap + int(fuel_laps) + band,
                "pit_eta_min": round(fuel_laps * pace / 60, 1) if stops else None,
                "confidence": round(conf, 2),
                "basis": ("MULTI_OBSERVATION" if stints_done >= 2 else
                          "SINGLE_OBSERVATION" if stints_done == 1 else "PRIOR_ONLY"),
                "anchor_uncertain": False,
                "stops_remaining": int(stops),
                "final_stop_fill_l": round(fill, 1) if fill else None,
                "save_to_skip_l_per_lap": save,
                "fuel_to_finish_l": round(laps_left * burn, 1),
                "_progress": sim_t / pace + offset,
            })
        # net-vs-us, computed the same way the engine does
        us = next((r for r in rows if r["car_idx"] == OUR_IDX), None)
        if us:
            our_debt = (us["stops_remaining"] or 0) * 55
            for r in rows:
                if r["car_idx"] == OUR_IDX:
                    continue
                gap = (us["_progress"] - r["_progress"]) * 140
                debt = (r["stops_remaining"] or 0) * 55 - our_debt
                net = gap + debt
                r["vs_us"] = {
                    "gap_s": round(gap, 1), "pit_debt_delta_s": round(debt, 1),
                    "net_vs_us_s": round(net, 1), "pace_trend_s": 0.0,
                    "stops_delta": (r["stops_remaining"] or 0) - (us["stops_remaining"] or 0),
                    "undercut_risk": -20 < gap < 0 and r["predicted_pit_lap_min"] <= us["predicted_pit_lap_min"] - 2,
                    "verdict": "AHEAD" if net > 10 else "BEHIND" if net < -10 else "FIGHT",
                }
        for r in rows:
            r.pop("_progress", None)
        hub.ingest({
            "type": "snapshot",
            "session": {
                "track_id": "spa", "track_display": "Circuit de Spa-Francorchamps",
                "session_type": "Race (demo feed)", "player_car_idx": OUR_IDX,
                "car_count": len(rows), "subsession_id": "demo",
                "time_remaining_s": remaining,
            },
            "predictions": rows,
            "ts": datetime.utcnow().isoformat() + "Z",
        })
        await asyncio.sleep(1)


@app.on_event("startup")
async def _start_demo() -> None:
    if DEMO:
        asyncio.create_task(_demo_feed())
