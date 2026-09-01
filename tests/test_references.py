"""
Reference-file loading: a row that cannot match must not look like one that
can. The race was run on a file containing "car_id": "VERIFY: bmwm4gt3evo",
which matched no CarPath, so every BMW M4 GT3 EVO in the field silently used
generic GT3 numbers (2.8 L/lap at a 120 s lap, against a real 2.41 at 89 s).
"""

import json
from pathlib import Path

import pytest

from run_pit_predictor import load_references, make_reference_provider
from pit_prediction import CompetitorState

REPO = Path(__file__).resolve().parents[1]

GOOD_ROW = {
    "car_id": "ferrari296gt3",
    "track_id": "spielberg gp",
    "tank_capacity_l": 104.0,
    "baseline_burn_l_per_lap": 2.5,
    "push_burn_l_per_lap": 2.62,
    "save_burn_l_per_lap": 2.2,
}


def _write(tmp_path, rows) -> str:
    path = tmp_path / "references.json"
    path.write_text(json.dumps(rows))
    return str(path)


def test_good_row_loads(tmp_path):
    refs, gaps = load_references(_write(tmp_path, [GOOD_ROW]))
    assert "ferrari296gt3|spielberg gp" in refs
    assert not gaps


def test_unresolved_car_id_is_dropped_and_reported(tmp_path):
    bad = dict(GOOD_ROW, car_id="VERIFY: bmwm4gt3evo")
    warnings = []
    refs, _ = load_references(_write(tmp_path, [GOOD_ROW, bad]), warnings.append)

    assert "VERIFY: bmwm4gt3evo|spielberg gp" not in refs
    assert len(refs) == 1, "the good row still loads"
    assert any("unresolved id" in w for w in warnings)


def test_wildcard_row_still_rejected(tmp_path):
    with pytest.raises(ValueError, match="wildcard"):
        load_references(_write(tmp_path, [dict(GOOD_ROW, track_id="*")]))


def test_missing_required_fields_are_reported_as_gaps(tmp_path):
    thin = {"car_id": "porsche992rgt3", "track_id": "spielberg gp"}
    refs, gaps = load_references(_write(tmp_path, [thin]))
    assert set(gaps["porsche992rgt3|spielberg gp"]) == {
        "tank_capacity_l",
        "baseline_burn_l_per_lap",
        "push_burn_l_per_lap",
        "save_burn_l_per_lap",
    }


def test_a_car_with_no_row_is_warned_about_once(tmp_path):
    refs, gaps = load_references(_write(tmp_path, [GOOD_ROW]))
    warnings = []
    provider = make_reference_provider(refs, "spielberg gp", gaps, warnings.append)

    state = CompetitorState(session_id="s", car_idx=1, car_id="bmwm4gt3evo")
    assert provider(state) is None
    assert provider(state) is None
    assert len(warnings) == 1
    assert "bmwm4gt3evo" in warnings[0]


def test_the_shipped_rbr_file_reports_its_unresolved_row():
    path = REPO / "client" / "references-vrs-s3-rbr.json"
    warnings = []
    refs, _ = load_references(str(path), warnings.append)

    assert any("unresolved id" in w for w in warnings), (
        "the BMW row that cost the race must be reported"
    )
    assert not any("VERIFY" in key for key in refs)
    assert len(refs) == 10, "the other ten rows still load"
