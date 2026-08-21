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
   anchors on the delta between the car's pace now (median of its last 5 green
   laps) and its OWN baseline pace (median of its last 20). The reference's
   `baseline_lap_time_s` is only the fallback for a car that has not completed
   a green lap yet. Anchoring the curve on that hand-entered constant is what
   the code used to do, and whenever the constant was wrong — or silently
   defaulted to 120.0 — every lap pinned at one end of the curve and stayed
   there, the save end under-reading burn by ~10% for a whole race. Deltas
   beyond ±5 s are traffic or an incident, not strategy, and return baseline
   burn. Medians, not means, so one lap lost in traffic moves nothing. Yellow
   laps weighted at `yellow_burn_multiplier` (~0.45), tracked separately.
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
9. **Measured mode (own car)**: `FuelLevel` + `PlayerCarIdx` in the frame
   put the player's car on ground truth — fuel on board straight from the
   gauge, burn from the median of the last 5 measured green-lap deltas
   (basis `MEASURED`, confidence 0.95, band from the actual sample spread).
   Validity is re-decided every frame: zero/absurd readings drop the mode
   instantly, and a gauge frozen >10 s while RACING/EXITING is a dead feed
   (teammate has the car). A constant reading IN_STALL is normal — engine
   off — so the freshness clock keeps running there. Measured laps also
   continuously EMA-calibrate `personal_burn_factor`, so fallback to
   inference starts from real consumption, not priors; stops observed on
   the gauge (entry captured while ENTERING, exit gauge = next stint
   anchor, fill = delta) replace the stall-time arithmetic and the
   fill-to-full snap. `estimated_fuel_l()` deliberately IGNORES the gauge —
   it is the shadow inference the OWN-CAR CHECK line scores against it.

Invariants that bugs love to violate:
- All fuel quantities are litres; never infer more than `tank_capacity_l`.
- Fuel accounting counts the lap in progress via `CarIdxLapDistPct`. Fuel
  burns continuously but the lap counter only ticks at the line, so dropping
  the fraction reads up to a full lap of fuel high — always optimistically,
  and worst exactly where it matters, on the lap a car must commit to pitting.
- Errors in this engine are not symmetric. Predicting a stop too late strands
  a car on track; too early costs a few seconds. Where a signal is ambiguous,
  resolve it toward more burn, not less.
- `last_stop_fuel_added_l` stores fuel ON BOARD at stint start (leftover +
  added), not the raw fill — the name is historical.
- Calibration divides THIS stop's fuel by THIS stint's laps.
- Key competitor identity by `cust_id` (stable), not `car_idx` (per-session).
- Lap counters can go backwards (session rotation, car resets) — handled in
  `_track_laps`; never assume monotonic laps.
- `predict()` may mutate state (missed-pit inference after a blackout);
  that's intentional self-healing, don't "fix" it into purity casually.

## Team races: identity and client placement

- `FuelLevel` is cockpit telemetry: live only on the ACTIVE driver's PC,
  frozen/zero for everyone else. For a two-driver lineup, run the client on
  BOTH drivers' PCs so measured mode follows whoever is in the car. Every
  uplink payload is stamped `client_id` + `driver_active` (`IsOnTrack` with
  a 60 s hold-down); the relay (`Hub._accept`) keeps the active client's
  feed and drops passive clients while an active one is fresh (15 s), so
  the two never interleave and handover at a swap is automatic. Payloads
  without the fields (legacy/solo) are accepted when no active client is
  around — a single-client spotter setup still works, it just never gets
  measured mode.
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

Rows keyed car_id + track_id. **Both ids are matched exactly and
case-sensitively against the session YAML** — there is no normalisation:

- `car_id` == `DriverInfo:Drivers[i]:CarPath`, e.g. `ferrari296gt3`
- `track_id` == `WeekendInfo:TrackName`, the track folder plus its config,
  lowercase and space-separated: `spa grandprix`, `spielberg gp`. NOT
  `TrackDisplayName` ("Red Bull Ring") and not the short name.

Track rows inherit missing fields from the car's `"track_id": "*"` wildcard
row. Car-level properties (tank, refuel rate, tyre time) live once in the
wildcard; track rows carry the burn/pace numbers. Give the wildcard a
car-level burn estimate too — it is what an unlisted track falls back on, and
a wildcard without one falls all the way to the generic 2.8 L/lap default.

A row you need but do not have used to fail silently: the engine dropped to
generic GT3 numbers and kept predicting at full confidence, ~30% off. The
runner now reports each distinct problem once, to both console and dashboard —
missing car, missing track row, required fields never set (`REQUIRED_REF_FIELDS`
in the runner), and unrecognised field names, since a misspelt field is
indistinguishable from an absent one. Keys prefixed `_` are ignored as
annotations, which is how you comment a JSON row.

`tank_capacity_l` must be the **BoP-effective** capacity
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
   100 min → 1 full stop; 170 min → 2 stops. Pin `CarIdxLapDistPct` to 0.0
   when asserting this — it holds for a car sampled at the line, mid-stint
   (~lap 18 of a 140 s / 3.4 L-per-lap / 104 L config). Sampled mid-lap the
   100-minute case needs 103.4 L of a 104 L tank and legitimately tips to
   "2 stops, second a 2 L splash, save-to-skip flagged".
9. compare_to_field: same-strategy rival ⇒ pit debt delta ≈ 0; short-filler
   ⇒ positive debt delta; close-behind rival w/ earlier window ⇒ undercut.
10. Pace anchor: a car lapping consistently at 90 s with a reference whose
    `baseline_lap_time_s` says 120 (or 140) must predict at BASELINE burn, not
    push or save — the curve anchors on the car's own median, not the row.
    Same car with a correct 90 s row must be unchanged. One 20 s traffic lap
    inside the 5-lap window must not move the burn estimate.
11. Measured mode: frames WITHOUT `FuelLevel`/`PlayerCarIdx` must reproduce
    scenarios 1–10 byte-identically, and player fuel present must leave
    competitor rows byte-identical. A player car on a 50 L partial load
    (104 L reference tank) must predict from the gauge — basis MEASURED,
    confidence 0.95, laps = (gauge − reserve) / measured burn — while
    `estimated_fuel_l()` (the shadow) still believes the full-tank story.
12. Gauge freeze (same value across >10 s of RACING frames) → measured mode
    drops, basis reverts, and the fallback burn stays ≈ the measured value
    (the measured laps EMA-calibrated `personal_burn_factor`). A live
    reading afterwards resumes MEASURED immediately.
13. Measured stop: entry gauge (captured while ENTERING) is the leftover,
    exit gauge is the next stint anchor (no fill-to-full snap), fill =
    delta and drives classification instead of stall time. The refuel jump
    must never appear in `measured_burns`, and one low-burn traffic lap
    must not move the measured median.

Dashboard/relay: run relay locally, drive with `--demo --server
http://localhost:8000 --token dev`, assert via `/api/state`. Relay
arbitration (`Hub._accept`): passive client dropped while an active one is
fresh; active client's own momentarily-inactive payloads kept; stale active
or field-less legacy payloads accepted.

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
- First stop of a deliberately underfuelled COMPETITOR is unpredictable by
  design; everything self-corrects at that stop. (Our own car no longer has
  this problem: measured mode reads the real load.)
- Whether a non-driving team member's sim reports the team car's FuelLevel
  is unverified — assumed dead (hence client-on-both-PCs). If a first team
  session shows it live on the passive PC too, the two-client setup still
  works; it is just redundant.
