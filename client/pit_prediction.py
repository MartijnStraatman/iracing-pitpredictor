"""
Pit Prediction Engine for iRacing GT3 endurance races.

Implements the design from the PitPrediction design doc:
  - Per-competitor pit state machine (RACING / ENTERING / IN_STALL / EXITING)
  - Stint + pit stop event emission with classification
  - Pace-to-burn interpolation (save / baseline / push anchors)
  - Per-driver burn factor calibration via EMA
  - Pit lap prediction with confidence bands

Integration:
    engine = PitPredictionEngine(reference_provider=my_ref_lookup)
    engine.on_pit_stop(lambda evt: store(evt))       # optional callbacks
    engine.on_stint(lambda evt: store(evt))

    # in your telemetry loop (~10 Hz):
    engine.process_frame(frame, now=datetime.utcnow())

    # whenever you want predictions:
    preds = engine.predict_all(session_time_remaining_s=5400)

`frame` is a dict-like exposing the pyirsdk CarIdx arrays, e.g. the irsdk
instance itself or a snapshot dict:
    frame['CarIdxOnPitRoad']    -> list[bool]
    frame['CarIdxTrackSurface'] -> list[int]
    frame['CarIdxLap']          -> list[int]
    frame['CarIdxLastLapTime']  -> list[float]
    frame['SessionFlags']       -> int (optional, for yellow detection)

No external dependencies. Persistence (Redis/DB) is left to the caller via
the event callbacks and `export_state()` / `import_state()`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# iRacing constants (irsdk_TrkLoc enum)
# ---------------------------------------------------------------------------

TRK_NOT_IN_WORLD = -1
TRK_OFF_TRACK = 0
TRK_IN_PIT_STALL = 1
TRK_APPROACHING_PITS = 2
TRK_ON_TRACK = 3

# irsdk_Flags bits commonly used for caution detection
FLAG_CAUTION = 0x4000
FLAG_CAUTION_WAVING = 0x8000
FLAG_YELLOW = 0x0008
FLAG_YELLOW_WAVING = 0x0100


class PitState(str, Enum):
    RACING = "RACING"
    ENTERING = "ENTERING"
    IN_STALL = "IN_STALL"
    EXITING = "EXITING"


class StopClass(str, Enum):
    FUEL_AND_TYRES = "FUEL_AND_TYRES"
    FUEL_ONLY = "FUEL_ONLY"
    TYRES_ONLY = "TYRES_ONLY"
    SPLASH = "SPLASH"
    DRIVE_THROUGH = "DRIVE_THROUGH"
    DAMAGE = "DAMAGE"


class StintEndReason(str, Enum):
    SCHEDULED_PIT = "SCHEDULED_PIT"
    SPLASH = "SPLASH"
    DAMAGE = "DAMAGE"
    PENALTY = "PENALTY"
    RACE_END = "RACE_END"


class PredictionBasis(str, Enum):
    PRIOR_ONLY = "PRIOR_ONLY"
    SINGLE_OBSERVATION = "SINGLE_OBSERVATION"
    MULTI_OBSERVATION = "MULTI_OBSERVATION"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CarTrackReference:
    car_id: str
    track_id: str
    track_config_id: str = ""
    tank_capacity_l: float = 100.0
    baseline_burn_l_per_lap: float = 2.8
    burn_stddev: float = 0.12
    push_burn_l_per_lap: float = 3.1
    save_burn_l_per_lap: float = 2.5
    yellow_burn_multiplier: float = 0.45
    baseline_lap_time_s: float = 120.0
    pit_lane_loss_s: float = 25.0
    refuel_rate_l_per_s: float = 2.5
    tyre_change_time_s: float = 22.0
    fixed_pit_overhead_s: float = 4.0
    sample_laps: int = 0
    measured_at: Optional[datetime] = None
    source: str = "manual"


@dataclass
class PitStopEvent:
    event_id: str
    session_id: str
    car_idx: int
    cust_id: int
    stint_id: str
    entry_ts: datetime
    stall_enter_ts: Optional[datetime]
    stall_exit_ts: Optional[datetime]
    exit_ts: datetime
    stall_duration_s: float
    inferred_fuel_added_l: float
    inferred_tyre_change: bool
    classified_as: StopClass


@dataclass
class StintEvent:
    event_id: str
    session_id: str
    car_idx: int
    cust_id: int
    stint_number: int
    start_lap: int
    end_lap: int
    laps_completed: int
    start_ts: datetime
    end_ts: datetime
    avg_lap_time_s: float
    green_laps: int
    yellow_laps: int
    inferred_start_fuel_l: float
    inferred_burn_l_per_lap: float
    stint_end_reason: StintEndReason


@dataclass
class PitPrediction:
    car_idx: int
    cust_id: int
    predicted_pit_lap: int
    predicted_pit_lap_min: int
    predicted_pit_lap_max: int
    predicted_pit_time: Optional[datetime]
    laps_of_fuel_remaining: float
    confidence: float
    basis: PredictionBasis
    last_calibrated_at: Optional[datetime]
    # race-finish strategy (requires session_time_remaining)
    stops_remaining: Optional[int] = None       # fuel stops needed to see the flag
    final_stop_fill_l: Optional[float] = None   # size of the last required fill
    fuel_to_finish_l: Optional[float] = None    # total fuel still needed
    save_to_skip_l_per_lap: Optional[float] = None  # feasible burn cut that drops a stop

    def to_dict(self) -> dict:
        d = asdict(self)
        d["basis"] = self.basis.value
        return d


@dataclass
class CompetitorState:
    session_id: str
    car_idx: int
    cust_id: int = -1
    car_id: str = ""
    class_id: str = ""

    # live standings, straight from telemetry -- 0 means "not yet classified".
    # Deliberately NOT persisted: position is a live fact, and a restored one
    # would be stale the instant the process comes back up.
    position: int = 0
    class_position: int = 0

    # stint tracking
    current_stint_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    stint_number: int = 1
    stint_start_lap: int = 1
    stint_start_ts: Optional[datetime] = None
    current_lap: int = 0
    last_lap_dist_pct: float = 0.0
    green_laps: int = 0
    yellow_laps: int = 0
    lap_times: List[float] = field(default_factory=list)  # green laps only

    # pit state machine
    pit_state: PitState = PitState.RACING
    pit_state_since: Optional[datetime] = None
    pit_entry_ts: Optional[datetime] = None
    stall_enter_ts: Optional[datetime] = None
    stall_exit_ts: Optional[datetime] = None
    stall_duration_s: float = 0.0
    pending_stint_end_lap: int = 0
    was_towed: bool = False

    # calibration
    personal_burn_factor: float = 1.0
    personal_burn_confidence: float = 0.0
    stints_observed: int = 0
    last_pit_lap: int = 0
    last_stop_fuel_added_l: float = 0.0
    last_calibrated_at: Optional[datetime] = None

    # crash / restart recovery
    anchor_uncertain: bool = False   # stint anchor may be stale after a restore
    _restored_lap: int = -1          # lap at snapshot time, for gap detection

    # rolling pace
    _last_seen_lap: int = -1
    _last_lap_was_yellow: bool = False

    ROLLING_WINDOW = 5

    @property
    def rolling_avg_lap_time_s(self) -> float:
        recent = self.lap_times[-self.ROLLING_WINDOW:]
        return sum(recent) / len(recent) if recent else 0.0

    @property
    def stint_avg_lap_time_s(self) -> float:
        return sum(self.lap_times) / len(self.lap_times) if self.lap_times else 0.0


# ---------------------------------------------------------------------------
# Pace-to-burn interpolation
# ---------------------------------------------------------------------------

SAVE_DELTA_S = 1.5   # saving stint ~1.5s off baseline pace
PUSH_DELTA_S = -0.8  # push lap ~0.8s under baseline


def reference_burn_for_pace(ref: CarTrackReference, avg_lap_time_s: float) -> float:
    """Expected burn/lap at the observed pace, piecewise-linear between anchors."""
    if avg_lap_time_s <= 0:
        return ref.baseline_burn_l_per_lap

    delta = avg_lap_time_s - ref.baseline_lap_time_s
    if delta >= 0:
        t = min(delta / SAVE_DELTA_S, 1.0)
        return ref.baseline_burn_l_per_lap * (1 - t) + ref.save_burn_l_per_lap * t
    t = min(abs(delta) / abs(PUSH_DELTA_S), 1.0)
    return ref.baseline_burn_l_per_lap * (1 - t) + ref.push_burn_l_per_lap * t


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

ReferenceProvider = Callable[[CompetitorState], Optional[CarTrackReference]]


class PitPredictionEngine:
    """
    Feed telemetry frames in; get StintEvents, PitStopEvents and predictions out.

    Args:
        reference_provider: callable(CompetitorState) -> CarTrackReference | None.
            Called lazily; return None to fall back to a generic GT3 default.
        session_id: identifier stamped onto emitted events.
        debounce_s: minimum dwell before a pit state transition is accepted.
        fuel_reserve_l: safety reserve subtracted from usable fuel.
        calibration_alpha: EMA weight for new burn-factor observations.
        prior_weight: weight of the full-tank assumption on stint 1 (underfuelled
            start handling): blended vs. observed as prior*w + observed*(1-w)
            is NOT used directly -- instead stint-1 observed burn is blended
            with the reference at this weight.
    """

    DEFAULT_REFERENCE = CarTrackReference(car_id="_generic_gt3", track_id="_unknown")
    FILL_TO_FULL_MARGIN_L = 6.0  # inferred board fuel within this of capacity => full

    def __init__(
        self,
        reference_provider: Optional[ReferenceProvider] = None,
        session_id: str = "",
        debounce_s: float = 0.5,
        fuel_reserve_l: float = 0.5,
        calibration_alpha: float = 0.6,
        stint1_prior_weight: float = 0.3,
    ):
        self.reference_provider = reference_provider
        self.session_id = session_id or str(uuid.uuid4())
        self.debounce = timedelta(seconds=debounce_s)
        self.fuel_reserve_l = fuel_reserve_l
        self.alpha = calibration_alpha
        self.stint1_prior_weight = stint1_prior_weight

        self.competitors: Dict[int, CompetitorState] = {}
        self._references: Dict[int, CarTrackReference] = {}
        self._pit_callbacks: List[Callable[[PitStopEvent], None]] = []
        self._stint_callbacks: List[Callable[[StintEvent], None]] = []
        self._session_started = False
        self._is_yellow = False

    # -- public API ---------------------------------------------------------

    def on_pit_stop(self, cb: Callable[[PitStopEvent], None]) -> None:
        self._pit_callbacks.append(cb)

    def on_stint(self, cb: Callable[[StintEvent], None]) -> None:
        self._stint_callbacks.append(cb)

    def register_competitor(
        self, car_idx: int, cust_id: int = -1, car_id: str = "", class_id: str = ""
    ) -> CompetitorState:
        """Optional: pre-register with identity from the session YAML."""
        state = self.competitors.get(car_idx)
        if state is None:
            state = CompetitorState(session_id=self.session_id, car_idx=car_idx)
            self.competitors[car_idx] = state
        state.cust_id = cust_id if cust_id != -1 else state.cust_id
        state.car_id = car_id or state.car_id
        state.class_id = class_id or state.class_id
        self._references.pop(car_idx, None)  # identity changed -> re-resolve ref
        return state

    def process_frame(self, frame, now: Optional[datetime] = None) -> None:
        """Call at ~10 Hz with a pyirsdk instance or snapshot dict."""
        now = now or datetime.utcnow()
        on_pit_arr = frame["CarIdxOnPitRoad"]
        surface_arr = frame["CarIdxTrackSurface"]
        lap_arr = frame["CarIdxLap"]
        last_lap_arr = self._safe_get(frame, "CarIdxLastLapTime")
        dist_arr = self._safe_get(frame, "CarIdxLapDistPct")
        # length-checked below: these two are absent in some session types
        pos_arr = self._safe_get(frame, "CarIdxPosition") or []
        class_pos_arr = self._safe_get(frame, "CarIdxClassPosition") or []

        flags = self._safe_get(frame, "SessionFlags")
        if flags is not None:
            self._is_yellow = bool(
                flags & (FLAG_CAUTION | FLAG_CAUTION_WAVING | FLAG_YELLOW | FLAG_YELLOW_WAVING)
            )

        for car_idx, on_pit in enumerate(on_pit_arr):
            surface = surface_arr[car_idx]
            if surface == TRK_NOT_IN_WORLD and car_idx not in self.competitors:
                continue  # empty slot

            state = self.competitors.get(car_idx)
            if state is None:
                state = CompetitorState(session_id=self.session_id, car_idx=car_idx)
                state.stint_start_ts = now
                self.competitors[car_idx] = state

            lap = lap_arr[car_idx]
            last_lap_time = last_lap_arr[car_idx] if last_lap_arr is not None else -1.0
            if dist_arr is not None and 0.0 <= dist_arr[car_idx] <= 1.0:
                state.last_lap_dist_pct = dist_arr[car_idx]
            # hold the last good value: iRacing briefly reports 0 for a car
            # that is off-world or between sessions, and blanking the tower
            # every time a rival clips a kerb would be worse than slightly old.
            if car_idx < len(pos_arr) and pos_arr[car_idx] > 0:
                state.position = pos_arr[car_idx]
            if car_idx < len(class_pos_arr) and class_pos_arr[car_idx] > 0:
                state.class_position = class_pos_arr[car_idx]
            self._track_laps(state, lap, last_lap_time)
            self._step_state_machine(state, bool(on_pit), surface, lap, now)

    def predict(
        self,
        car_idx: int,
        session_time_remaining_s: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> Optional[PitPrediction]:
        state = self.competitors.get(car_idx)
        if state is None or state.current_lap <= 0:
            return None
        ref = self._reference_for(state)
        return self._predict(state, ref, session_time_remaining_s, now or datetime.utcnow())

    def predict_all(
        self,
        session_time_remaining_s: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> List[PitPrediction]:
        now = now or datetime.utcnow()
        preds = []
        for idx in self.competitors:
            p = self.predict(idx, session_time_remaining_s, now)
            if p:
                preds.append(p)
        return preds

    def estimated_fuel_l(self, car_idx: int) -> Optional[float]:
        """Model's current fuel-on-board estimate for a car. For validating
        the inference against ground truth (your own car's FuelLevel)."""
        state = self.competitors.get(car_idx)
        if state is None or state.current_lap <= 0:
            return None
        ref = self._reference_for(state)
        pace = state.rolling_avg_lap_time_s or ref.baseline_lap_time_s
        burn = reference_burn_for_pace(ref, pace) * state.personal_burn_factor
        return self._fuel_remaining(state, ref, burn)

    def export_state(self) -> dict:
        """Full serializable snapshot (calibration + stint anchors), by cust_id."""
        out = {}
        for s in self.competitors.values():
            key = s.cust_id if s.cust_id != -1 else f"idx:{s.car_idx}"
            out[str(key)] = {
                "personal_burn_factor": s.personal_burn_factor,
                "personal_burn_confidence": s.personal_burn_confidence,
                "stints_observed": s.stints_observed,
                "last_pit_lap": s.last_pit_lap,
                "last_stop_fuel_added_l": s.last_stop_fuel_added_l,
                "stint_number": s.stint_number,
                "current_lap": s.current_lap,
                "green_laps": s.green_laps,
                "yellow_laps": s.yellow_laps,
            }
        return out

    def import_state(self, snapshot: dict, same_session: bool = False) -> None:
        """
        Restore from a snapshot, keyed by cust_id.

        same_session=False (default): warm-start burn factors only — use when
        the snapshot came from a *previous race* (different subsession).

        same_session=True: full mid-race recovery — also restores stint anchors
        (last pit lap, fuel added, lap counters). Use after a process restart
        or iRacing crash within the SAME subsession. Laps that elapsed while
        the process was down are detected on the first live frame; if the gap
        is large the anchor is marked uncertain (wider bands) and self-heals
        via missed-pit inference or the car's next observed stop.
        """
        for s in self.competitors.values():
            data = snapshot.get(str(s.cust_id)) or snapshot.get(f"idx:{s.car_idx}")
            if not data:
                continue
            s.personal_burn_factor = data["personal_burn_factor"]
            if same_session:
                s.personal_burn_confidence = data["personal_burn_confidence"]
                s.stints_observed = data.get("stints_observed", 0)
                s.last_pit_lap = data.get("last_pit_lap", 0)
                s.last_stop_fuel_added_l = data.get("last_stop_fuel_added_l", 0.0)
                s.stint_number = data.get("stint_number", 1)
                s.green_laps = data.get("green_laps", 0)
                s.yellow_laps = data.get("yellow_laps", 0)
                s._restored_lap = data.get("current_lap", -1)
            else:
                # decay imported confidence -- conditions may have changed
                s.personal_burn_confidence = min(0.5, data["personal_burn_confidence"])

    # -- lap / pace tracking ------------------------------------------------

    def _track_laps(self, state: CompetitorState, lap: int, last_lap_time: float) -> None:
        # Lap counter went BACKWARDS: session rotated (practice -> qualy -> race)
        # or the car was reset. Restart stint tracking at the new lap; keep
        # calibration (burn factors), drop the now-meaningless anchor.
        if 0 <= lap < state._last_seen_lap - 1:
            state._last_seen_lap = lap
            state.current_lap = lap
            state.stint_start_lap = lap
            state.green_laps = 0
            state.yellow_laps = 0
            state.lap_times = []
            state.last_pit_lap = 0
            state.last_stop_fuel_added_l = 0.0
            state.stint_number = 1
            state.anchor_uncertain = False
            return
        if lap <= state._last_seen_lap or lap <= 0:
            return
        # first live lap after a same-session restore: how long were we blind?
        if state._restored_lap >= 0:
            gap = lap - state._restored_lap
            if gap > 2:
                # long blackout: the car may have pitted unseen. Count the
                # missed laps as green so fuel accounting stays continuous,
                # widen bands, and let missed-pit inference re-anchor.
                state.anchor_uncertain = True
                state.green_laps += max(0, gap - 1)
            state._last_seen_lap = lap
            state.current_lap = lap
            state._restored_lap = -1
            return
        # crossed the line (possibly multiple laps if frames were dropped)
        state.current_lap = lap
        if state._last_seen_lap >= 0 and state.pit_state == PitState.RACING:
            if self._is_yellow:
                state.yellow_laps += 1
            else:
                state.green_laps += 1
                if last_lap_time and last_lap_time > 0:
                    state.lap_times.append(last_lap_time)
                    if len(state.lap_times) > 50:
                        state.lap_times = state.lap_times[-50:]
        state._last_seen_lap = lap

    # -- state machine ------------------------------------------------------

    def _step_state_machine(
        self, state: CompetitorState, on_pit: bool, surface: int, lap: int, now: datetime
    ) -> None:
        towed = surface in (TRK_NOT_IN_WORLD, TRK_OFF_TRACK) and state.pit_state != PitState.RACING

        if state.pit_state == PitState.RACING:
            if on_pit and self._dwell_ok(state, now):
                self._set_state(state, PitState.ENTERING, now)
                state.pit_entry_ts = now
                state.pending_stint_end_lap = lap
            elif surface == TRK_NOT_IN_WORLD and state.stint_start_ts is not None:
                # towed from track: treat as heading to stall, flag damage
                self._set_state(state, PitState.ENTERING, now)
                state.pit_entry_ts = now
                state.pending_stint_end_lap = lap
                state.was_towed = True

        elif state.pit_state == PitState.ENTERING:
            if surface == TRK_IN_PIT_STALL or towed:
                self._set_state(state, PitState.IN_STALL, now)
                state.stall_enter_ts = now
                state.was_towed = state.was_towed or towed
            elif not on_pit and self._dwell_ok(state, now):
                # drive-through: no stall, back racing
                self._emit_drive_through(state, now)
                self._set_state(state, PitState.RACING, now)
                state.pit_entry_ts = None

        elif state.pit_state == PitState.IN_STALL:
            if surface != TRK_IN_PIT_STALL and self._dwell_ok(state, now):
                self._set_state(state, PitState.EXITING, now)
                state.stall_exit_ts = now
                if state.stall_enter_ts:
                    state.stall_duration_s = (now - state.stall_enter_ts).total_seconds()

        elif state.pit_state == PitState.EXITING:
            if not on_pit:
                self._close_stint(state, lap, now)
                self._set_state(state, PitState.RACING, now)

    def _set_state(self, state: CompetitorState, new: PitState, now: datetime) -> None:
        state.pit_state = new
        state.pit_state_since = now

    def _dwell_ok(self, state: CompetitorState, now: datetime) -> bool:
        return state.pit_state_since is None or (now - state.pit_state_since) >= self.debounce

    # -- event emission + calibration ---------------------------------------

    def _emit_drive_through(self, state: CompetitorState, now: datetime) -> None:
        evt = PitStopEvent(
            event_id=str(uuid.uuid4()),
            session_id=self.session_id,
            car_idx=state.car_idx,
            cust_id=state.cust_id,
            stint_id=state.current_stint_id,
            entry_ts=state.pit_entry_ts or now,
            stall_enter_ts=None,
            stall_exit_ts=None,
            exit_ts=now,
            stall_duration_s=0.0,
            inferred_fuel_added_l=0.0,
            inferred_tyre_change=False,
            classified_as=StopClass.DRIVE_THROUGH,
        )
        for cb in self._pit_callbacks:
            cb(evt)

    def _close_stint(self, state: CompetitorState, lap: int, now: datetime) -> None:
        ref = self._reference_for(state)

        # Estimate fuel still on board at pit entry (model-derived leftover).
        pace = state.stint_avg_lap_time_s or ref.baseline_lap_time_s
        entry_burn = reference_burn_for_pace(ref, pace) * state.personal_burn_factor
        leftover = max(0.0, min(self._fuel_remaining(state, ref, entry_burn),
                                ref.tank_capacity_l))

        pit_evt = self._build_pit_event(state, ref, now)
        stint_evt = self._build_stint_event(state, ref, pit_evt, now)

        if pit_evt.classified_as in (StopClass.FUEL_AND_TYRES, StopClass.FUEL_ONLY):
            self._calibrate(state, ref, stint_evt, pit_evt, now)

        for cb in self._pit_callbacks:
            cb(pit_evt)
        for cb in self._stint_callbacks:
            cb(stint_evt)

        # Fuel on board at the start of the next stint: what they arrived with
        # plus what they took, capped at the tank. If that lands close to
        # capacity, they filled to full -- snap to it (absorbs inference noise).
        start_fuel = min(ref.tank_capacity_l, leftover + pit_evt.inferred_fuel_added_l)
        if start_fuel >= ref.tank_capacity_l - self.FILL_TO_FULL_MARGIN_L:
            start_fuel = ref.tank_capacity_l
        if pit_evt.classified_as == StopClass.DAMAGE:
            start_fuel = leftover  # tow/repair stop: no confident fuel info

        # roll into new stint
        state.last_pit_lap = lap
        state.last_stop_fuel_added_l = start_fuel  # semantics: fuel ON BOARD at stint start
        state.stint_number += 1
        state.current_stint_id = str(uuid.uuid4())
        state.stint_start_lap = lap
        state.stint_start_ts = now
        state.green_laps = 0
        state.yellow_laps = 0
        state.lap_times = []
        state.pit_entry_ts = None
        state.stall_enter_ts = None
        state.stall_exit_ts = None
        state.stall_duration_s = 0.0
        state.was_towed = False
        state.anchor_uncertain = False  # real stop observed -> anchor is solid again

    def _build_pit_event(
        self, state: CompetitorState, ref: CarTrackReference, now: datetime
    ) -> PitStopEvent:
        dur = state.stall_duration_s
        service_time = max(0.0, dur - ref.fixed_pit_overhead_s)

        # tyre change detection: is the stop long enough for tyres at all?
        min_tyre_stop = ref.tyre_change_time_s * 0.8
        tyre_change = service_time >= min_tyre_stop

        fuel_time = service_time - (ref.tyre_change_time_s if tyre_change else 0.0)
        fuel_added = max(0.0, fuel_time * ref.refuel_rate_l_per_s)
        # A stop can be long for reasons that aren't fuel (driver swap, repairs,
        # waiting out a penalty) -- never infer more than the tank can hold.
        fuel_added = min(fuel_added, ref.tank_capacity_l)
        # refuel and tyre change run concurrently in most GT3 series? In iRacing
        # they are sequential for fixed stops; if fuel_time went negative the
        # stop was tyres-only length.
        if tyre_change and fuel_time <= 0:
            fuel_added = 0.0

        if state.was_towed:
            cls = StopClass.DAMAGE
            fuel_added = 0.0
        elif fuel_added <= 0 and tyre_change:
            cls = StopClass.TYRES_ONLY
        elif fuel_added > 0 and tyre_change:
            cls = StopClass.FUEL_AND_TYRES
        elif 0 < fuel_added <= 0.25 * ref.tank_capacity_l:
            cls = StopClass.SPLASH
        elif fuel_added > 0:
            cls = StopClass.FUEL_ONLY
        else:
            cls = StopClass.DAMAGE  # zero service time, wasn't a drive-through

        return PitStopEvent(
            event_id=str(uuid.uuid4()),
            session_id=self.session_id,
            car_idx=state.car_idx,
            cust_id=state.cust_id,
            stint_id=state.current_stint_id,
            entry_ts=state.pit_entry_ts or now,
            stall_enter_ts=state.stall_enter_ts,
            stall_exit_ts=state.stall_exit_ts,
            exit_ts=now,
            stall_duration_s=dur,
            inferred_fuel_added_l=round(fuel_added, 2),
            inferred_tyre_change=tyre_change,
            classified_as=cls,
        )

    def _build_stint_event(
        self,
        state: CompetitorState,
        ref: CarTrackReference,
        pit_evt: PitStopEvent,
        now: datetime,
    ) -> StintEvent:
        effective_laps = state.green_laps + state.yellow_laps * ref.yellow_burn_multiplier
        if state.stint_number == 1:
            start_fuel = ref.tank_capacity_l
        else:
            start_fuel = state.last_stop_fuel_added_l
        burn = start_fuel / effective_laps if effective_laps > 0 else 0.0

        reason = {
            StopClass.SPLASH: StintEndReason.SPLASH,
            StopClass.DAMAGE: StintEndReason.DAMAGE,
            StopClass.DRIVE_THROUGH: StintEndReason.PENALTY,
        }.get(pit_evt.classified_as, StintEndReason.SCHEDULED_PIT)

        return StintEvent(
            event_id=str(uuid.uuid4()),
            session_id=self.session_id,
            car_idx=state.car_idx,
            cust_id=state.cust_id,
            stint_number=state.stint_number,
            start_lap=state.stint_start_lap,
            end_lap=state.pending_stint_end_lap,
            laps_completed=max(0, state.pending_stint_end_lap - state.stint_start_lap),
            start_ts=state.stint_start_ts or now,
            end_ts=now,
            avg_lap_time_s=round(state.stint_avg_lap_time_s, 3),
            green_laps=state.green_laps,
            yellow_laps=state.yellow_laps,
            inferred_start_fuel_l=round(start_fuel, 2),
            inferred_burn_l_per_lap=round(burn, 3),
            stint_end_reason=reason,
        )

    def _calibrate(
        self,
        state: CompetitorState,
        ref: CarTrackReference,
        stint: StintEvent,
        pit: PitStopEvent,
        now: datetime,
    ) -> None:
        effective_laps = stint.green_laps + stint.yellow_laps * ref.yellow_burn_multiplier
        if effective_laps < 3:
            return  # too short to be informative

        if stint.stint_number == 1:
            observed_burn = ref.tank_capacity_l / effective_laps
            # underfuelled-start hedge: blend toward reference
            pace_ref = reference_burn_for_pace(ref, stint.avg_lap_time_s)
            observed_burn = (
                self.stint1_prior_weight * pace_ref
                + (1 - self.stint1_prior_weight) * observed_burn
            )
        else:
            # Fuel added at the stop ENDING this stint replaces what the stint
            # burned -- valid when the car fills to full at consecutive stops
            # (arrives similarly empty each time). Guard: skip short fills,
            # where added fuel no longer tracks consumption.
            if pit.inferred_fuel_added_l < 0.5 * ref.tank_capacity_l:
                return
            observed_burn = pit.inferred_fuel_added_l / effective_laps

        pace_adjusted_ref = reference_burn_for_pace(ref, stint.avg_lap_time_s)
        if pace_adjusted_ref <= 0:
            return
        new_factor = observed_burn / pace_adjusted_ref
        # reject implausible factors (bad classification, partial data)
        if not 0.6 <= new_factor <= 1.5:
            return

        state.personal_burn_factor = (
            self.alpha * new_factor + (1 - self.alpha) * state.personal_burn_factor
        )
        state.stints_observed += 1
        state.personal_burn_confidence = min(1.0, state.stints_observed / 3)
        state.last_calibrated_at = now

    # -- prediction ---------------------------------------------------------

    def _predict(
        self,
        state: CompetitorState,
        ref: CarTrackReference,
        session_time_remaining_s: Optional[float],
        now: datetime,
    ) -> PitPrediction:
        pace = state.rolling_avg_lap_time_s or ref.baseline_lap_time_s
        ref_burn = reference_burn_for_pace(ref, pace)
        effective_burn = ref_burn * state.personal_burn_factor
        if self._is_yellow:
            # burn right now is reduced; projection still uses green burn for
            # remaining laps, which is the conservative (earlier) estimate
            pass

        fuel_remaining = self._fuel_remaining(state, ref, effective_burn)

        # Missed-pit inference: if the model says the car ran out of fuel more
        # than a lap ago, it must have pitted while we weren't watching
        # (process restart / iRacing crash blackout). Re-anchor by advancing
        # the pit lap one estimated stint at a time and assuming a full fill.
        if effective_burn > 0:
            guard = 0
            while fuel_remaining < -1.0 * effective_burn and guard < 5:
                est_stint_laps = max(1, int(ref.tank_capacity_l / effective_burn))
                base = state.last_pit_lap if state.last_pit_lap > 0 else 0
                state.last_pit_lap = min(state.current_lap, base + est_stint_laps)
                state.last_stop_fuel_added_l = ref.tank_capacity_l
                state.green_laps = max(0, state.current_lap - state.last_pit_lap)
                state.yellow_laps = 0
                state.anchor_uncertain = True
                fuel_remaining = self._fuel_remaining(state, ref, effective_burn)
                guard += 1

        usable = fuel_remaining - self.fuel_reserve_l
        laps_of_fuel = max(0.0, usable / effective_burn) if effective_burn > 0 else 0.0

        burn_unc = ref.burn_stddev * (2.0 - state.personal_burn_confidence)
        if state.anchor_uncertain:
            burn_unc *= 2.0  # stale anchor -> much wider band
        low = max(0.0, usable / (effective_burn + burn_unc)) if usable > 0 else 0.0
        high = max(0.0, usable / max(0.1, effective_burn - burn_unc)) if usable > 0 else 0.0

        pit_time = None
        if pace > 0:
            pit_time = now + timedelta(seconds=laps_of_fuel * pace)
            if session_time_remaining_s is not None:
                if laps_of_fuel * pace > session_time_remaining_s:
                    pit_time = None  # will finish on fuel, no stop expected

        basis = (
            PredictionBasis.PRIOR_ONLY
            if state.stints_observed == 0
            else PredictionBasis.SINGLE_OBSERVATION
            if state.stints_observed == 1
            else PredictionBasis.MULTI_OBSERVATION
        )

        confidence = state.personal_burn_confidence
        if state.anchor_uncertain:
            confidence = min(confidence, 0.25)

        # --- Race-finish strategy: stops remaining and last-fill size ---
        stops_remaining = final_fill = fuel_to_finish = save_to_skip = None
        if session_time_remaining_s is not None and session_time_remaining_s > 0 and pace > 0:
            # laps THIS car still runs, at its own pace (+1 for the white-flag lap)
            laps_remaining = session_time_remaining_s / pace + 1
            fuel_to_finish = laps_remaining * effective_burn + self.fuel_reserve_l
            shortfall = fuel_to_finish - max(0.0, fuel_remaining)
            if shortfall <= 0:
                stops_remaining = 0
            else:
                stops_remaining = int(-(-shortfall // ref.tank_capacity_l))  # ceil
                final_fill = shortfall - (stops_remaining - 1) * ref.tank_capacity_l
                # Save-to-skip: the burn cut per lap that erases the final
                # required fill. Only reported when physically achievable
                # (within ~90% of the car's lift-and-coast delta) -- that is
                # the rival to watch for a fuel-save stint.
                if laps_remaining > 1:
                    cut = final_fill / laps_remaining
                    max_save = ref.baseline_burn_l_per_lap - ref.save_burn_l_per_lap
                    if 0 < cut <= max_save * 0.9:
                        save_to_skip = round(cut, 2)
            fuel_to_finish = round(fuel_to_finish, 1)
            final_fill = round(final_fill, 1) if final_fill is not None else None

        return PitPrediction(
            car_idx=state.car_idx,
            cust_id=state.cust_id,
            predicted_pit_lap=state.current_lap + int(laps_of_fuel),
            predicted_pit_lap_min=state.current_lap + int(low),
            predicted_pit_lap_max=state.current_lap + int(high),
            predicted_pit_time=pit_time,
            laps_of_fuel_remaining=round(laps_of_fuel, 2),
            confidence=round(confidence, 2),
            basis=basis,
            last_calibrated_at=state.last_calibrated_at,
            stops_remaining=stops_remaining,
            final_stop_fill_l=final_fill,
            fuel_to_finish_l=fuel_to_finish,
            save_to_skip_l_per_lap=save_to_skip,
        )

    def _fuel_remaining(
        self, state: CompetitorState, ref: CarTrackReference, effective_burn: float
    ) -> float:
        yellow_credit = state.yellow_laps * (1 - ref.yellow_burn_multiplier)
        if state.stints_observed == 0 and state.last_pit_lap == 0:
            laps_done = max(0, state.current_lap - 1) - yellow_credit
            return ref.tank_capacity_l - laps_done * effective_burn
        laps_since_pit = max(0, state.current_lap - state.last_pit_lap) - yellow_credit
        start_fuel = state.last_stop_fuel_added_l or ref.tank_capacity_l
        return start_fuel - laps_since_pit * effective_burn


    # -- strategic comparison ------------------------------------------------

    SPLASH_TYRE_CUTOFF_L = 30.0  # fills below this: no tyres taken

    def _remaining_pit_time_s(self, p: PitPrediction, ref: CarTrackReference) -> float:
        """Total future time loss to pit stops needed to reach the flag."""
        if not p.stops_remaining:
            return 0.0
        fills = [ref.tank_capacity_l] * (p.stops_remaining - 1)
        fills.append(p.final_stop_fill_l if p.final_stop_fill_l is not None
                     else ref.tank_capacity_l)
        total = 0.0
        for fill in fills:
            service = ref.fixed_pit_overhead_s + fill / ref.refuel_rate_l_per_s
            if fill >= self.SPLASH_TYRE_CUTOFF_L:
                service += ref.tyre_change_time_s
            total += ref.pit_lane_loss_s + service
        return total

    def compare_to_field(
        self,
        own_idx: int,
        session_time_remaining_s: Optional[float],
        now: Optional[datetime] = None,
    ) -> Dict[int, dict]:
        """
        Net standing vs every same-class rival once ALL remaining stops are
        served. Keyed by rival car_idx. Positive numbers are good for us.

        net_vs_us_s = track gap now + (their remaining pit debt - ours).
        Pace trend is reported separately: it is an extrapolation, and race
        engineers should weigh it with more suspicion than the pit-debt math.
        """
        now = now or datetime.utcnow()
        own = self.competitors.get(own_idx)
        own_pred = self.predict(own_idx, session_time_remaining_s, now)
        if own is None or own_pred is None:
            return {}
        own_ref = self._reference_for(own)
        own_pit_debt = self._remaining_pit_time_s(own_pred, own_ref)
        own_pace = own.rolling_avg_lap_time_s or own_ref.baseline_lap_time_s
        own_prog = own.current_lap + own.last_lap_dist_pct
        laps_rem = (session_time_remaining_s / own_pace
                    if session_time_remaining_s and own_pace else 0.0)

        out: Dict[int, dict] = {}
        for idx, rival in self.competitors.items():
            if idx == own_idx:
                continue
            if rival.class_id and own.class_id and rival.class_id != own.class_id:
                continue
            p = self.predict(idx, session_time_remaining_s, now)
            if p is None:
                continue
            ref = self._reference_for(rival)
            pace = rival.rolling_avg_lap_time_s or ref.baseline_lap_time_s
            # >0: we are ahead on track (seconds)
            gap_s = (own_prog - (rival.current_lap + rival.last_lap_dist_pct)) * pace
            pit_debt = self._remaining_pit_time_s(p, ref)
            debt_delta = pit_debt - own_pit_debt          # >0: they owe more lane time
            net = gap_s + debt_delta
            pace_trend = (pace - own_pace) * laps_rem     # >0: we pull away by the flag
            # Undercut risk: rival within striking distance behind us whose
            # window opens at least 2 laps before ours.
            undercut = (
                0.0 < gap_s < own_ref.pit_lane_loss_s + 10.0
                and p.predicted_pit_lap_min <= own_pred.predicted_pit_lap_min - 2
            )
            stops_delta = ((p.stops_remaining or 0) - (own_pred.stops_remaining or 0))
            out[idx] = {
                "gap_s": round(gap_s, 1),
                "pit_debt_delta_s": round(debt_delta, 1),
                "net_vs_us_s": round(net, 1),
                "pace_trend_s": round(pace_trend, 1),
                "stops_delta": stops_delta,
                "undercut_risk": undercut,
                "verdict": ("AHEAD" if net > 10 else "BEHIND" if net < -10 else "FIGHT"),
            }
        return out

    # -- helpers ------------------------------------------------------------

    def _reference_for(self, state: CompetitorState) -> CarTrackReference:
        ref = self._references.get(state.car_idx)
        if ref is None:
            if self.reference_provider:
                ref = self.reference_provider(state)
            ref = ref or self.DEFAULT_REFERENCE
            self._references[state.car_idx] = ref
        return ref

    @staticmethod
    def _safe_get(frame, key):
        try:
            return frame[key]
        except (KeyError, TypeError):
            return None
