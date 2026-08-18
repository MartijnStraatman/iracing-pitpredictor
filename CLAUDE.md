# CLAUDE.md

PitWall — iRacing GT3 endurance pit stop predictor. Infers competitor fuel
state (not exposed by iRacing) from stint lengths and pit stall durations,
predicts pit windows, computes race-finish strategy, and shares live
predictions with the team via a web dashboard.

## Architecture

Two deployables, deliberately split so raw 10 Hz telemetry never crosses the
network — only computed results do:

```
iRacing PC (Windows)                      OVH server (Docker)
┌─────────────────────────┐   HTTPS      ┌──────────────────────┐
│ run_pit_predictor.py    │  snapshots   │ relay/server.py      │
│  pyirsdk @ 10 Hz        │  1/s, ~few   │  FastAPI             │
│  ├─ pit_prediction.py   │──────────────▶  POST /api/ingest    │
│  │   (engine, no deps)  │  events      │  GET  /api/stream SSE│──▶ team
│  └─ console display     │  as they     │  GET  /  dashboard   │   browsers
│     + Uplink thread     │  happen      │  /data/events.jsonl  │
└─────────────────────────┘              └──────────────────────┘
```

- `client/pit_prediction.py` — the prediction engine. **Zero dependencies,
  pure stdlib.** Keep it that way; it must run anywhere and be trivially
  unit-testable.
- `client/run_pit_predictor.py` — pyirsdk runner: telemetry loop, console
  display, ground-truth validator, relay uplink (background thread, stdlib
  urllib). Only runtime dep: `pyirsdk` (Windows-only, guarded import inside
  `IRacingSource`). Everything else must work without it (`--demo`).
- `relay/server.py` — FastAPI relay. Holds latest snapshot in memory, fans
  out over SSE, appends everything to JSONL for post-race analysis. Also
  contains `_demo_feed()` (~100 lines, gated behind the `DEMO` env var and
  otherwise inert) which fabricates a sample race for deployment testing —
  it does NOT import or exercise the engine, so never treat its output as
  evidence about prediction behaviour.
- `relay/static/index.html` — single-file dashboard (no build step, no
  framework). Dark timing-tower design; keep it a single file.

## Domain model (read this before touching the engine)

Competitor fuel is NOT observable in iRacing telemetry (`FuelLevel` is
player-only). The engine infers it:

1. **Pit state machine** per car: RACING → ENTERING → IN_STALL → EXITING,
   driven by `CarIdxOnPitRoad` + `CarIdxTrackSurface`, 500 ms debounce.
   Emits `PitStopEvent` + `StintEvent` on completion.
2. **Stop classification** from stall duration: solve
   `stall ≈ overhead + tyres + fuel/refuel_rate` for fuel added, clamped to
   tank capacity. Classes: FUEL_AND_TYRES, FUEL_ONLY, TYRES_ONLY, SPLASH,
   DRIVE_THROUGH, DAMAGE (tow). Only the first two calibrate.
3. **Calibration**: fuel added at the stop ENDING a stint ≈ fuel burned in
   that stint (valid when filling to full both ends — short fills <50% tank
   are skipped). `personal_burn_factor` = EMA (α=0.6) of observed/reference
   burn. Stint 1 assumes a full tank, blended 30% toward reference to hedge
   underfuelled starts.
4. **Pace→burn interpolation**: piecewise linear between save/baseline/push
   anchors on lap-time delta, clamped both ends. Yellow laps weighted at
   `yellow_burn_multiplier` (~0.45), tracked separately.
5. **Fuel-on-board accounting**: next stint start = leftover-at-entry +
   fuel added, capped at tank; snaps to full if within 6 L (fill-to-full).
6. **Prediction**: fuel remaining / effective burn → pit lap + confidence
   band (band width from `burn_stddev` × (2 − confidence)).
7. **Race-finish strategy**: laps remaining at the car's own pace → fuel to
   flag → stops remaining, final fill size, save-to-skip threshold.
8. **Field comparison** (`compare_to_field`): net vs each same-class rival =
   track gap + (their remaining pit debt − ours); undercut flag when a rival
   is close behind with a window opening ≥2 laps earlier. Pace trend is
   reported but deliberately NOT folded into net (extrapolation ≠ math).

Invariants that bugs love to violate:
- All fuel quantities are litres; never infer more than `tank_capacity_l`.
- `last_stop_fuel_added_l` stores fuel ON BOARD at stint start (leftover +
  added), not the raw fill — the name is historical.
- Calibration divides THIS stop's fuel by THIS stint's laps.
- Key competitor identity by `cust_id` (stable), not `car_idx` (per-session).
- Lap counters can go backwards (session rotation, car resets) — handled in
  `_track_laps`; never assume monotonic laps.
- `predict()` may mutate state (missed-pit inference after a blackout);
  that's intentional self-healing, don't "fix" it into purity casually.

## Team races: identity and client placement

- Run ONE client instance, on any connected team member's PC — a non-driving
  spotter is ideal (their sim never closes for a driver swap). Never run two;
  both would push interleaved snapshots to the relay.
- "Our car" = telemetry `PlayerCarIdx`, which points to the TEAM entry for
  every connected team member, not just the active driver.
- Persistence keys use `_persist_id()`: TeamID when present (team sessions),
  else UserID — because the roster's UserID for a car changes at every
  driver swap, while TeamID is stable for the whole race. Solo sessions have
  TeamID 0 and fall back to UserID, preserving per-driver profiles.
- Client-PC handoff: recovery state is parked on the relay (`type: "state"`
  payloads → `GET /api/client-state`, ingest-token auth). A fresh PC joining
  the same subsession restores from the relay automatically when its local
  drivers.json is missing or stale. State payloads are never broadcast to
  viewers and never written to events.jsonl.

## Crash / restart recovery

`--state drivers.json` snapshots full anchors (atomic tmp+rename) every
second, keyed by cust_id with subsession ID + timestamp. On startup:
same subsession + <6 h old → full restore; otherwise burn factors only
(confidence capped 0.5). After long blackouts, missed-pit inference
re-anchors cars that pitted unseen; `anchor_uncertain` widens bands until
their next observed stop clears it.

## Reference data (`references.json`)

Rows keyed car_id + track_id. Track rows inherit missing fields from the
car's `"track_id": "*"` wildcard row — car-level properties (tank, refuel
rate, tyre time) live once in the wildcard, track rows carry only burn/pace
numbers. `tank_capacity_l` must be the **BoP-effective** capacity
(`DriverCarFuelMaxLtr × DriverCarMaxFuelPct`); the runner warns on mismatch
for the player's own car model.

## Commands

```bash
# engine has no test framework yet — tests were run as inline scripts.
# If adding tests, use pytest under tests/ and mirror the scenarios below.

# client, simulated session (no iRacing needed) -- runs the REAL engine
# against synthetic telemetry. Use this to test prediction behaviour.
cd client && python run_pit_predictor.py --demo

# relay, self-generating sample race -- dashboard goes live with no client.
# Use this to test a DEPLOYMENT (URL, TLS, phone rendering) only; the feed is
# plain arithmetic in server.py (_demo_feed), NOT the engine, so it proves
# nothing about prediction correctness. Never leave DEMO set in production:
# it overwrites a real client's snapshots every second.
cd relay && DEMO=1 INGEST_TOKEN=dev python -m uvicorn server:app

# client, live:
python run_pit_predictor.py --refs references.json --state drivers.json \
    --server https://pitwall.example.com --token $INGEST_TOKEN

# relay, local dev:
cd relay && INGEST_TOKEN=dev python -m uvicorn server:app --reload

# relay, production:
cd relay && cp .env.example .env && docker compose up -d --build
```

## Testing conventions

The engine is tested by synthesizing telemetry frames — a `frame(on_pit,
surface, lap)` dict factory driven through `process_frame` with a stepped
`datetime`. Scenarios that MUST keep passing (re-create as pytest cases when
adding a test suite):

1. Full stint + 60 s stop → FUEL_AND_TYRES classification, ~correct litres,
   calibration factor in 0.9–1.2, band tightens post-stop.
2. 130 s stop (driver swap) → fuel clamped ≤ tank.
3. Splash (short stall) → SPLASH class, NO calibration, start fuel =
   leftover + splash (not snapped to full).
4. Drive-through (pit road, no stall) → DRIVE_THROUGH, stint continues.
5. Same-session restore, 1-lap gap → identical predictions.
6. Same-session restore, 30-lap blackout → missed-pit inference re-anchors,
   `anchor_uncertain` set, cleared by next real stop.
7. Lap counter reset mid-stream → stint tracking restarts, factors kept.
8. Strategy: 20 min left → 0 stops; 60 min → 1 stop w/ save-to-skip;
   100 min → 1 full stop; 170 min → 2 stops.
9. compare_to_field: same-strategy rival ⇒ pit debt delta ≈ 0; short-filler
   ⇒ positive debt delta; close-behind rival w/ earlier window ⇒ undercut.

Dashboard/relay: run relay locally, drive with `--demo --server
http://localhost:8000 --token dev`, assert via `/api/state`.

## Style & constraints

- Python 3.10+; dataclasses + enums; type hints on public APIs.
- Engine: stdlib only. Runner: stdlib + pyirsdk. Relay: fastapi + uvicorn.
- Dashboard: vanilla JS + CSS in one file, SSE (not WebSocket), fonts from
  Google Fonts with system fallbacks, respects prefers-reduced-motion.
- Timestamps: naive UTC (`datetime.utcnow()`) throughout; durations in
  seconds; the dashboard receives ETAs as minutes (`pit_eta_min`), never
  absolute times, to avoid clock-skew between PC/server/viewers.
- Snapshot payload rows spread `PitPrediction.to_dict()` — adding a field to
  the dataclass automatically ships it to the dashboard.
- Never block the telemetry loop: network I/O goes through the Uplink
  thread; newest snapshot wins, stale ones are shed.

## Known gaps / roadmap

- Tyre-change detection threshold (0.8 × tyre_change_time_s) and
  splash-vs-tyres cutoff (30 L) need validation against real logged stops.
- Series with concurrent fuel+tyre service break the sequential stall-time
  model — make it a per-series reference flag if needed.
- events.jsonl is collected but unanalysed: post-race backtest comparing
  predicted vs actual pit laps should tune `burn_stddev` and EMA α.
- Track temp / weather regression on burn rate (iRacing exposes both).
- First stop of a deliberately underfuelled car is unpredictable by design;
  everything self-corrects at that stop.
