"""
VRS datapack -> references.json importer (personal use).

VRS has no public API; the web app loads datapack fuel/lap data through
internal JSON endpoints behind your login. This tool works from a HAR
capture of your own browser session -- no credentials stored, no live
scraping, and it only ever sees data your subscription already shows you.

Step 1 -- capture (once per series/season):
  1. Chrome/Edge/Firefox: open devtools (F12) -> Network tab.
  2. Tick "Preserve log". Filter: Fetch/XHR.
  3. Log in to virtualracingschool.com, open the datapack, and click
     through each track's page so the fuel/lap-time tables are shown.
  4. Right-click the request list -> "Save all as HAR (with content)".

Step 2 -- find the fuel data in the capture:
  python tools/vrs_import.py explore capture.har
This lists every JSON response whose body mentions fuel/lap fields and
dumps each to vrs_bodies/<n>.json for inspection.

Step 3 -- emit reference rows from the capture itself:
  python tools/vrs_import.py emit capture.har --car-id mercedesamgevogt3 \
      --track-id "spielberg gp"

Or fully automated -- give the track name as shown in the VRS UI and the
tool calls VRS live. The HAR is needed ONCE: the first fetch persists the
request templates and cookie to vrs_bodies/vrs_seed.json, and every later
run needs no HAR at all:
  python tools/vrs_import.py fetch capture.har --track "red bull"   # once
  python tools/vrs_import.py fetch --track "spa"                    # after
It looks the track up in the datapack, fetches every dry session's laps,
and prints a ready reference row (car/track already mapped to iRacing ids
where known). Add --out client/references-vrs.json to also write the rows
to a NEW timestamped file (references-vrs-20260823-1511.json) -- existing
files are never touched; combine snapshots into your live references.json
yourself, or point the client's --refs straight at a snapshot.

All cars in the series at once: capture the datapack PICKER page (the list
of the series' datapacks per car) once -- harvesting it adds every plan's
id to the seed -- then:
  python tools/vrs_import.py plans                      # what is known
  python tools/vrs_import.py fetch --track "spa" --all-cars \
      --series "gt sprint" --season "2026 s3"
One reference row is emitted per car's datapack. --series (substring of the
datapack name) keeps other series' datapacks out of the loop; seasons
differ in BoP and track list, so --season pins the loop to one season --
without it the newest season among the matching plans is used (and
announced).

When the session cookie eventually expires, don't re-capture -- copy the
Cookie header of any request from a logged-in tab (devtools -> Network ->
click a request -> Request Headers -> Cookie) and pass it once:
  python tools/vrs_import.py fetch --track "spa" --cookie '<paste>'
(or set VRS_COOKIE). It is saved back into the seed file for future runs.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from statistics import median

# Signals that a JSON body is about fuel / stint data, not UI chrome.
INTERESTING = re.compile(
    r"fuel|burn|consumption|lap_?time|laptime|stint", re.IGNORECASE
)
# Anything from a VRS host is dumped even without keyword hits -- the field
# names are unknown, so the keyword filter must not be the gatekeeper there.
VRS_HOST = re.compile(r"virtualracingschool|vrs", re.IGNORECASE)
# Noise: analytics, fonts, source maps, embedded video players (a YouTube
# caption track mentioning "fuel" is not telemetry).
SKIP_URL = re.compile(
    r"google-analytics|gtag|sentry|youtube\.|googlevideo\.|ytimg\."
    r"|\.js$|\.css$|\.map$"
)


def _har_entries(path: Path):
    har = json.loads(path.read_text(encoding="utf-8"))
    return har.get("log", {}).get("entries", [])


def _body_text(entry: dict) -> str:
    content = entry.get("response", {}).get("content", {})
    text = content.get("text")
    if text is None:
        return ""
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8", "replace")
        except Exception:
            return ""
    return text


def _parse_response(body: str):
    """Plain JSON, or a GWT-RPC payload ('//OK[...]' -- what app.vrs.racing
    speaks): strip the prefix and parse the rest as a JSON array."""
    text = body.lstrip()
    kind = "json"
    if text.startswith("//OK"):
        text, kind = text[4:], "gwt-rpc"
    elif text.startswith("//EX"):
        return None, "gwt-error"
    try:
        return json.loads(text), kind
    except ValueError:
        return None, kind


def explore(har_path: Path, out_dir: Path) -> int:
    out_dir.mkdir(exist_ok=True)
    hits = 0
    for i, entry in enumerate(_har_entries(har_path)):
        url = entry.get("request", {}).get("url", "")
        if SKIP_URL.search(url):
            continue
        mime = entry.get("response", {}).get("content", {}).get("mimeType", "")
        body = _body_text(entry)
        if not body or (
            "json" not in mime
            and not body.lstrip().startswith(("{", "[", "//OK"))
        ):
            continue
        matches = sorted(set(m.group(0).lower() for m in INTERESTING.finditer(body)))
        if not matches and not VRS_HOST.search(url):
            continue
        parsed, kind = _parse_response(body)
        if parsed is None:
            continue
        # GWT-RPC requests are positional: the METHOD is named in the POST
        # body, not the URL -- keep it so calls can be told apart.
        post = entry.get("request", {}).get("postData", {}).get("text", "")
        method = "|".join(post.split("|")[5:8]) if kind == "gwt-rpc" else ""
        if not matches:
            matches = ["(none -- dumped because it came from a VRS host)"]
        hits += 1
        dump = out_dir / f"{hits:02d}.json"
        dump.write_text(
            json.dumps({"url": url, "kind": kind, "method": method,
                        "request": post[:2000], "response": parsed},
                       indent=2)[:2_000_000],
            encoding="utf-8",
        )
        print(f"[{hits:02d}] {kind:8s} {method or url[:80]}")
        print(f"     mentions: {', '.join(matches[:8])}   -> {dump}")
    if hits == 0:
        print(
            "No JSON responses mentioning fuel/lap fields found.\n"
            "Make sure 'Preserve log' was on BEFORE opening the datapack pages,\n"
            "and that you exported 'with content'."
        )
    else:
        print(
            f"\n{hits} candidate response(s) dumped to {out_dir}/ -- inspect them "
            "(or hand the smallest one that clearly contains per-lap fuel to "
            "Claude) so the `emit` mapping can be written."
        )
    return 0 if hits else 1


LAP_CLASS = "com.VirtualRacingSchool.WebApp.shared.Lap/"
KEY_KINDS = ("Driver", "Platform", "CarTrack", "Session")
ID_RE = re.compile(r"^[A-Za-z0-9$_]{1,12}$")

# VRS track display name -> iRacing track_id. Entries marked VERIFY are a
# best guess; the client's startup warning quotes the true string on a miss.
TRACK_MAP = {
    "Red Bull Ring (Grand Prix)": "spielberg gp",
    "Circuit de Spa-Francorchamps (Grand Prix Pits)": "spa grandprix",
    "Circuit des 24 Heures du Mans (24 Heures du Mans)": "lemans full",
    "Suzuka International Racing Course (Grand Prix)": "suzuka grandprix",
    "Watkins Glen International (Boot)": "watkinsglen 2021 boot",
    "Virginia International Raceway (Full Course)": "virginia full",
    "Hockenheimring Baden-Württemberg (Grand Prix)": "VERIFY: hockenheim gp",
    "Indianapolis Motor Speedway (Road Course)": "VERIFY: indianapolis road",
    "Oran Park Raceway (Grand Prix)": "VERIFY: oranpark gp",
    "St. Petersburg Grand Prix (Grand Prix)": "VERIFY: stpetersburg gp",
}
CAR_MAP = {
    "BMW M4 GT3 EVO": "VERIFY: bmwm4gt3evo",  # EVO is its own iRacing car
    "Mercedes-AMG GT3": "mercedesamgevogt3",
    "Ferrari 296": "ferrari296gt3",
    "Porsche 911 GT3 R": "porsche992rgt3",
    "BMW M4": "bmwm4gt3",
    "Audi R8": "audir8lmsevo2gt3",
    "Lamborghini": "lamborghinievogt3",
    "McLaren 720S": "mclaren720sgt3",
    "Acura NSX": "acuransxevo22gt3",
    "Ford Mustang": "fordmustanggt3",
    "Corvette Z06": "chevyvettez06rgt3",
    "Aston Martin Vantage": "amvantageevogt3",
}


def _decode_laps(payload: list) -> list[dict]:
    """Pull (lap_time_s, fuel_l, tank_l) out of one getLaps GWT-RPC response.

    GWT serialization is positional (field names never cross the wire), so
    fields are identified by physics instead of names:
      - lap time: the float immediately before a float-array ('[F') whose
        elements SUM to it -- that array is the sector times
      - fuel: the adjacent float pair (litres x, tank-fraction y) whose
        implied tank x/y lands in 60..140 L (GT3 territory)
    """
    table = next((x for x in payload if isinstance(x, list)), [])
    lap_ref = next(
        (i + 1 for i, s in enumerate(table)
         if isinstance(s, str) and s.startswith(LAP_CLASS)),
        None,
    )
    fl_ref = next(
        (i + 1 for i, s in enumerate(table)
         if isinstance(s, str) and s.startswith("[F/")),
        None,
    )
    if lap_ref is None or fl_ref is None:
        return []
    tokens = list(reversed(payload))[3:]  # skip version, flags, string table
    marks = [i for i, t in enumerate(tokens) if t == lap_ref]
    laps = []
    for a, b in zip(marks, marks[1:] + [len(tokens)]):
        rec = tokens[a:b]
        lap_s = None
        for i, t in enumerate(rec[:-2]):
            if t != fl_ref or i == 0:
                continue
            cand, n = rec[i - 1], rec[i + 1]
            if not (isinstance(cand, float) and 60 < cand < 600):
                continue
            if not (isinstance(n, int) and 0 < n <= 20 and i + 1 + n < len(rec)):
                continue
            sectors = rec[i + 2 : i + 2 + n]
            if all(isinstance(s, float) for s in sectors) and abs(
                sum(sectors) - cand
            ) < 0.5:
                lap_s = cand
                break
        floats = [(i, t) for i, t in enumerate(rec) if isinstance(t, float)]
        fuel = tank = None
        for (i, x), (j, y) in zip(floats, floats[1:]):
            if j == i + 1 and 1 < x < 200 and 0 < y < 1 and 60 < x / y < 140:
                fuel, tank = x, x / y
                break
        if lap_s is not None and fuel is not None:
            laps.append({"lap_s": lap_s, "fuel_l": fuel, "tank_l": tank})
    return laps


def emit(har_path: Path, car_id: str, track_id: str) -> int:
    from statistics import median

    found = 0
    seen: set = set()
    for entry in _har_entries(har_path):
        url = entry.get("request", {}).get("url", "")
        post = entry.get("request", {}).get("postData", {}).get("text", "")
        if not VRS_HOST.search(url) or "getLaps" not in post:
            continue
        if post in seen:  # HARs often hold the same exchange twice
            continue
        seen.add(post)
        parsed, kind = _parse_response(_body_text(entry))
        if kind != "gwt-rpc" or not isinstance(parsed, list):
            continue
        laps = _decode_laps(parsed)
        if len(laps) < 4:
            continue
        found += 1
        # burn = fuel drop lap-to-lap; refuels/resets show as rises -> dropped
        burns = [
            a["fuel_l"] - b["fuel_l"]
            for a, b in zip(laps, laps[1:])
            if 0 < a["fuel_l"] - b["fuel_l"] < 10
        ]
        times = [l["lap_s"] for l in laps]
        tank = round(median(l["tank_l"] for l in laps), 1)
        if not burns:
            print(f"[{found}] {len(laps)} laps but no usable fuel deltas -- skipped")
            continue
        burn = round(median(burns), 2)
        row = {
            "_source": f"VRS datapack, {len(laps)} laps, {len(burns)} fuel deltas",
            "car_id": car_id or "FILL_ME (see references docs)",
            "track_id": track_id or "FILL_ME (see references docs)",
            "tank_capacity_l": tank,
            "refuel_rate_l_per_s": 2.5,
            "tyre_change_time_s": 22.0,
            "fixed_pit_overhead_s": 4.0,
            "baseline_burn_l_per_lap": burn,
            "push_burn_l_per_lap": round(burn * 1.05, 2),
            "save_burn_l_per_lap": round(burn * 0.88, 2),
            "baseline_lap_time_s": round(median(times), 1),
            "burn_stddev": 0.12,
        }
        print(f"\n# getLaps call {found}: {len(laps)} laps, "
              f"median {median(times):.1f}s, burn {burn} L/lap, tank ~{tank} L")
        print(json.dumps(row, indent=1))
    if not found:
        print("no decodable getLaps responses in this capture")
    else:
        print(
            "\nNotes: VRS laps are pro pace -- treat the burn as slightly "
            "push-side for your own splits. Check wet/pre-BoP sessions were "
            "not in the capture (session labels in the VRS UI say so)."
        )
    return 0 if found else 1


def _harvest_seed(har_path: Path) -> dict:
    """Auth headers, request templates, and any datapack plan list found in
    a HAR of the user's own logged-in VRS session."""
    seed = {"headers": {}, "templates": {}, "plans": {}}
    keep = {"cookie", "x-gwt-permutation", "x-gwt-module-base",
            "content-type", "user-agent", "origin", "referer"}
    for entry in _har_entries(har_path):
        req = entry.get("request", {})
        url = req.get("url", "")
        post = req.get("postData", {}).get("text", "")
        if "app.vrs.racing" not in url or not post.startswith("7|"):
            continue
        if not seed["headers"]:
            for h in req.get("headers", []):
                if h["name"].lower() in keep:
                    seed["headers"][h["name"]] = h["value"]
        # each GWT service lives at its own endpoint path -- keep the URL
        # together with the body it was captured with
        for method in ("getTelemetryPlan", "getPlanSessions", "getLaps"):
            if f"|{method}|" in post and method not in seed["templates"]:
                seed["templates"][method] = (url, post)
        # a response listing 2+ "(... Season N)" plans is the datapack
        # picker -- harvest every plan's name + id from it
        parsed, kind = _parse_response(_body_text(entry))
        if kind == "gwt-rpc" and isinstance(parsed, list):
            seed["plans"].update(_plan_list(parsed))
    return seed


def _plan_list(payload: list) -> dict:
    """{plan display name: TelemetryPlan id} from a datapack-picker response.

    The picker lists datapacks as pseudo-Driver records shaped
      [Driver, Key, Key, 0, ref('TelemetryPlan'), <id>, 0, <id>, ref(name), ...]
    so the pairing is read out of each record, never guessed by proximity
    (an earlier nearest-id heuristic paired names with their neighbours'
    ids and produced a silently wrong catalog)."""
    stream, table = _stream_and_table(payload)
    drv_ref = next(
        (i + 1 for i, s in enumerate(table) if isinstance(s, str)
         and s.startswith("com.VirtualRacingSchool.WebApp.shared.Driver/")),
        None,
    )
    plan_kind = (table.index("TelemetryPlan") + 1
                 if "TelemetryPlan" in table else None)
    if drv_ref is None or plan_kind is None:
        return {}
    marks = [i for i, t in enumerate(stream) if t == drv_ref]
    out = {}
    for a, b in zip(marks, marks[1:] + [len(stream)]):
        rec = stream[a:b]
        pid = name = None
        for i, t in enumerate(rec):
            if (t == plan_kind and i + 1 < len(rec)
                    and isinstance(rec[i + 1], str)
                    and ID_RE.match(rec[i + 1])):
                pid = pid or rec[i + 1]
            if (isinstance(t, int) and not isinstance(t, bool)
                    and 1 <= t <= len(table)):
                v = table[t - 1]
                if (isinstance(v, str)
                        and re.search(r"Season \d+\)$", v)):
                    name = name or v
        if pid and name:
            out[name] = pid
    return out


# anchored to the repo (next to tools/), not the current directory
SEED_PATH = Path(__file__).resolve().parent.parent / "vrs_bodies" / "vrs_seed.json"


def _load_seed(arg: str | None, cookie: str | None) -> dict:
    """Seed from a HAR (first time; persisted for reuse), else from the saved
    seed file. The HAR is only ever needed once per season -- when the session
    cookie expires, pass a fresh one with --cookie/VRS_COOKIE instead."""
    import os

    if arg and arg.endswith(".har"):
        seed = _harvest_seed(Path(arg))
        # merge over any earlier seed: a partial capture (e.g. just the
        # datapack picker page) adds plans without losing the lap templates
        if SEED_PATH.exists():
            old = json.loads(SEED_PATH.read_text(encoding="utf-8"))
            seed["templates"] = {**old.get("templates", {}), **seed["templates"]}
            seed["plans"] = {**old.get("plans", {}), **seed.get("plans", {})}
            seed["headers"] = seed["headers"] or old.get("headers", {})
        missing = [m for m in ("getTelemetryPlan", "getPlanSessions", "getLaps")
                   if m not in seed["templates"]]
        if missing or not seed["headers"].get("Cookie"):
            raise SystemExit(
                f"seed is missing {missing or 'the Cookie header'} -- capture "
                "a HAR while browsing one datapack track (see the module "
                "docstring)."
            )
        seed["templates"] = {k: tuple(v) for k, v in seed["templates"].items()}
        SEED_PATH.parent.mkdir(exist_ok=True)
        SEED_PATH.write_text(json.dumps(seed, indent=1), encoding="utf-8")
        try:
            SEED_PATH.chmod(0o600)  # holds the session cookie
        except OSError:
            pass
        print(f"(seed saved to {SEED_PATH} -- future runs don't need the HAR; "
              f"{len(seed.get('plans', {}))} datapack plan(s) known)")
    else:
        path = Path(arg) if arg else SEED_PATH
        if not path.exists():
            raise SystemExit(
                f"no saved seed at {path} -- run once with a HAR file first "
                "(see the module docstring for the one-time capture)."
            )
        seed = json.loads(path.read_text(encoding="utf-8"))
        # templates are stored as lists by JSON round-tripping
        seed["templates"] = {k: tuple(v) for k, v in seed["templates"].items()}
    cookie = cookie or os.environ.get("VRS_COOKIE")
    if cookie:
        seed["headers"]["Cookie"] = cookie
        if SEED_PATH.exists():
            saved = json.loads(SEED_PATH.read_text(encoding="utf-8"))
            saved["headers"]["Cookie"] = cookie
            SEED_PATH.write_text(json.dumps(saved, indent=1), encoding="utf-8")
            print("(new cookie saved to the seed file)")
    return seed


def _call(seed: dict, template: tuple[str, str] | None = None,
          body: str | None = None, url: str | None = None):
    if template is not None:
        url, body = template
    req = urllib.request.Request(url, data=body.encode("utf-8"),
                                 headers=seed["headers"], method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    parsed, kind = _parse_response(text)
    if kind == "gwt-error" or parsed is None:
        raise SystemExit(
            "VRS rejected the call (session cookie probably expired) -- "
            "re-export a fresh HAR from a logged-in browser tab."
        )
    return parsed


def _stream_and_table(payload: list):
    table = next((x for x in payload if isinstance(x, list)), [])
    return list(reversed(payload))[3:], table


def _plan_tracks(payload: list) -> dict:
    """getTelemetryPlan -> {track display name: track short id}."""
    stream, table = _stream_and_table(payload)
    names = [s for s in table if isinstance(s, str) and "(" in s
             and "/" not in s and "Season" not in s]
    out = {}
    for nm in names:
        ref = table.index(nm) + 1
        for p in (i for i, t in enumerate(stream) if t == ref):
            near = [t for t in stream[max(0, p - 30):p + 30]
                    if isinstance(t, str) and ID_RE.match(t) and len(t) <= 3]
            if near:
                out[nm] = near[0]
            break
    return out


def _plan_sessions(payload: list) -> list[dict]:
    """getPlanSessions -> [{Driver, Platform, CarTrack, Session, labels}]."""
    stream, table = _stream_and_table(payload)
    chains, current = [], {}
    positions = {}
    for i, t in enumerate(stream[:-1]):
        nxt = stream[i + 1]
        if (isinstance(t, int) and not isinstance(t, bool)
                and 1 <= t <= len(table) and isinstance(nxt, str)
                and ID_RE.match(nxt) and table[t - 1] in KEY_KINDS):
            current[table[t - 1]] = nxt
            if len(current) == 4:
                current["_pos"] = i
                if not any(all(c[k] == current[k] for k in KEY_KINDS)
                           for c in chains):
                    chains.append(current)
                current = {}
    # attach the nearest label strings (dates, wet/dry notes) to each chain
    label_refs = [(table.index(s) + 1, s) for s in table
                  if isinstance(s, str) and "/" not in s and "." not in s
                  and re.search(r"\b(19|20)\d\d\b|wet|dry|bop|usage",
                                s, re.IGNORECASE)]
    for ref, s in label_refs:
        for p in (i for i, t in enumerate(stream) if t == ref):
            best = min(chains, key=lambda c: abs(c["_pos"] - p), default=None)
            if best is not None and abs(best["_pos"] - p) < 80:
                best.setdefault("labels", []).append(s)
    return chains


def _build_getlaps(template: str, chain: dict) -> str:
    """Swap the Driver/Platform/CarTrack/Session ids of the captured getLaps
    request for the target session's chain. Ids are inline literals following
    their kind's string-table ref."""
    parts = template.split("|")
    n_table = int(parts[2])
    table = parts[3:3 + n_table]
    kind_ref = {name: table.index(name) + 1 for name in KEY_KINDS
                if name in table}
    for i in range(3 + n_table, len(parts) - 1):
        for name, ref in kind_ref.items():
            if parts[i] == str(ref) and ID_RE.match(parts[i + 1] or "-"):
                parts[i + 1] = chain[name]
    return "|".join(parts)


def _sub_ids(body: str, ids: dict) -> str:
    """Replace the inline id literal that follows each kind's string-table
    ref, for every {kind name: new id} given."""
    parts = body.split("|")
    n_table = int(parts[2])
    table = parts[3:3 + n_table]
    kind_ref = {k: table.index(k) + 1 for k in ids if k in table}
    for i in range(3 + n_table, len(parts) - 1):
        for name, ref in kind_ref.items():
            if parts[i] == str(ref) and ID_RE.match(parts[i + 1] or "-"):
                parts[i + 1] = ids[name]
    return "|".join(parts)


def _plan_season(name: str) -> tuple[int, int]:
    """'... (2026 Season 3)' -> (2026, 3); (0, 0) when unparsable."""
    m = re.search(r"\((\d{4}) [Ss]eason (\d+)\)", name)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def fetch(seed_arg: str | None, track_query: str, cookie: str | None = None,
          plan_id: str | None = None, all_cars: bool = False,
          season: str | None = None, series: str | None = None,
          out: str | None = None) -> int:
    seed = _load_seed(seed_arg, cookie)
    if all_cars:
        plans = seed.get("plans", {})
        if not plans:
            print(
                "no datapack plan list in the seed yet. Capture a HAR while "
                "opening the VRS page that LISTS the series' datapacks (the "
                "car picker), then run once with that HAR:\n"
                "  python tools/vrs_import.py fetch picker.har --track '...' "
                "--all-cars"
            )
            return 1
        if series:
            plans = {n: p for n, p in plans.items()
                     if series.lower() in n.lower()}
            if not plans:
                print(f"no datapack plan matches series '{series}'. Known:")
                for n in seed["plans"]:
                    print(f"  - {n}")
                return 1
        if season:
            # '2026 s3' / '2026 season 3' -> exact (year, n) match;
            # anything else falls back to a substring match on the name
            m = re.search(r"(\d{4})\D+(\d+)", season)
            if m:
                want = (int(m.group(1)), int(m.group(2)))
                plans = {n: p for n, p in plans.items()
                         if _plan_season(n) == want}
            else:
                plans = {n: p for n, p in plans.items()
                         if season.lower() in n.lower()}
            if not plans:
                print(f"no datapack plan matches season '{season}'. Known:")
                for n in seed["plans"]:
                    print(f"  - {n}")
                return 1
        else:
            # seasons differ in BoP and track list -- never mix them.
            # Default to the newest season present and say so.
            latest = max(_plan_season(n) for n in plans)
            if latest != (0, 0):
                plans = {n: p for n, p in plans.items()
                         if _plan_season(n) == latest}
                print(f"(no --season given: using the newest, "
                      f"{latest[0]} Season {latest[1]})")
        worst, rows = 0, []
        for name, pid in sorted(plans.items()):
            print(f"\n===== {name}")
            st, row = _fetch_one(seed, track_query, pid)
            worst = max(worst, st)
            if row:
                rows.append(row)
        if out and rows:
            _write_out(Path(out), rows)
        return worst
    st, row = _fetch_one(seed, track_query, plan_id)
    if out and row:
        _write_out(Path(out), [row])
    return st


def _write_out(path: Path, rows: list[dict]) -> None:
    """Write the fetched rows to a NEW file with a timestamp in the name --
    never merges, never overwrites. references-vrs.json becomes
    references-vrs-20260823-1436.json; combine into your live
    references.json by hand (or point --refs straight at the snapshot)."""
    from datetime import datetime

    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M")
    out = path.with_name(f"{path.stem}-{stamp}{path.suffix or '.json'}")

    # A row whose id could not be resolved must not sit in the same file as
    # the good ones. It never matches a CarPath, so the car quietly runs on
    # generic GT3 numbers while the file looks complete -- which is how a
    # whole class of cars raced on a 2.8 L/lap prior at a track that burns
    # 2.41. They go to a separate file the client will not load.
    clean = [r for r in rows if not _unresolved(r)]
    unresolved = [r for r in rows if _unresolved(r)]

    out.write_text(
        "[\n" + ",\n".join("  " + json.dumps(r) for r in clean) + "\n]\n",
        encoding="utf-8",
    )
    print(f"\nwrote {len(clean)} row(s) to {out}")

    if unresolved:
        review = out.with_name(f"{out.stem}.needs-review{out.suffix}")
        review.write_text(
            "[\n" + ",\n".join("  " + json.dumps(r) for r in unresolved) + "\n]\n",
            encoding="utf-8",
        )
        print(f"\n{len(unresolved)} row(s) have an id this tool could not "
              f"resolve; they are NOT in the file above.")
        for r in unresolved:
            print(f"  car_id={r['car_id']!r} track_id={r['track_id']!r}")
        print(f"held in {review}\n"
              "Fix the ids against a live session and merge them in by hand:\n"
              "  python -c \"import irsdk;ir=irsdk.IRSDK();ir.startup();"
              "print(repr(ir['WeekendInfo']['TrackName']));"
              "print(sorted({d['CarPath'] for d in ir['DriverInfo']['Drivers']}))\"")


def _unresolved(row: dict) -> bool:
    return any(k in (str(row["car_id"]) + str(row["track_id"]))
               for k in ("FILL_ME", "VERIFY"))


def list_plans(seed_arg: str | None) -> int:
    seed = _load_seed(seed_arg, None)
    plans = seed.get("plans", {})
    if not plans:
        print("no datapack plans in the seed yet -- capture the picker page "
              "once (see the module docstring).")
        return 1
    for name, pid in sorted(plans.items(),
                            key=lambda kv: (_plan_season(kv[0]), kv[0]),
                            reverse=True):
        y, n = _plan_season(name)
        tag = f"{y} S{n}" if y else "?"
        print(f"  [{tag:8s}] {pid:12s} {name}")
    return 0


def _fetch_one(seed: dict, track_query: str,
               plan_id: str | None = None) -> tuple[int, dict | None]:
    plan_tpl = seed["templates"]["getTelemetryPlan"]
    sess_tpl = seed["templates"]["getPlanSessions"]
    if plan_id:
        plan_tpl = (plan_tpl[0], _sub_ids(plan_tpl[1], {"TelemetryPlan": plan_id}))
        sess_tpl = (sess_tpl[0], _sub_ids(sess_tpl[1], {"TelemetryPlan": plan_id}))
    plan = _call(seed, template=plan_tpl)
    tracks = _plan_tracks(plan)
    _, plan_table = _stream_and_table(plan)
    plan_name = next((s for s in plan_table if isinstance(s, str)
                      and "Season" in s), "")
    q = track_query.lower()
    matches = {nm: sid for nm, sid in tracks.items() if q in nm.lower()}
    if len(matches) != 1:
        print(f"datapack: {plan_name}")
        print(f"'{track_query}' matched {len(matches)} of these tracks:")
        for nm in tracks:
            print(f"  - {nm}")
        return 1, None
    (track_name, short_id), = matches.items()

    sessions = _plan_sessions(_call(seed, template=sess_tpl))
    # CarTrack id = car prefix + track short id; learn the prefix from any
    # session whose CarTrack ends in a known track short id
    prefix = next(
        (c["CarTrack"][: -len(sid)]
         for c in sessions for sid in tracks.values()
         if c.get("CarTrack", "").endswith(sid) and len(c["CarTrack"]) > len(sid)),
        None,
    )
    if prefix is None:
        print("could not derive the CarTrack id prefix from the plan sessions")
        return 1, None
    cartrack = prefix + short_id
    wanted = [c for c in sessions if c["CarTrack"] == cartrack]
    dry = [c for c in wanted if not any(
        re.search(r"wet|bop", l, re.IGNORECASE) for l in c.get("labels", []))]
    print(f"datapack: {plan_name}")
    print(f"track: {track_name}  (CarTrack {cartrack})  "
          f"sessions: {len(wanted)} total, {len(dry)} dry")
    if not dry:
        return 1, None

    all_laps = []
    for c in dry[:8]:
        time.sleep(0.5)  # be polite: this is someone else's backend
        laps_url, laps_tpl = seed["templates"]["getLaps"]
        laps = _decode_laps(_call(seed, body=_build_getlaps(laps_tpl, c),
                                  url=laps_url))
        labels = ", ".join(c.get("labels", [])[:3]) or "?"
        print(f"  session {c['Session']}: {len(laps)} laps   [{labels}]")
        all_laps.extend(laps)

    burns = [a["fuel_l"] - b["fuel_l"] for a, b in zip(all_laps, all_laps[1:])
             if 0 < a["fuel_l"] - b["fuel_l"] < 10]
    if len(burns) < 4:
        print("not enough usable fuel deltas")
        return 1, None
    burn = round(median(burns), 2)
    car_id = next(
        (cid for nm, cid in sorted(CAR_MAP.items(), key=lambda kv: -len(kv[0]))
         if nm in plan_name),
        "FILL_ME",
    )
    row = {
        "_source": f"VRS {plan_name}, {track_name}: "
                   f"{len(all_laps)} laps, {len(burns)} fuel deltas",
        "car_id": car_id,
        "track_id": TRACK_MAP.get(track_name, "FILL_ME"),
        "tank_capacity_l": round(median(l["tank_l"] for l in all_laps), 1),
        "refuel_rate_l_per_s": 2.5,
        "tyre_change_time_s": 22.0,
        "fixed_pit_overhead_s": 4.0,
        "baseline_burn_l_per_lap": burn,
        "push_burn_l_per_lap": round(burn * 1.05, 2),
        "save_burn_l_per_lap": round(burn * 0.88, 2),
        "baseline_lap_time_s": round(median(l["lap_s"] for l in all_laps), 1),
        "burn_stddev": 0.12,
    }
    print("\n" + json.dumps(row, indent=1))
    return 0, row


def main() -> int:
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "explore":
        return explore(Path(args[1]), Path("vrs_bodies"))
    if args and args[0] == "fetch":
        track = args[args.index("--track") + 1] if "--track" in args else ""
        cookie = args[args.index("--cookie") + 1] if "--cookie" in args else None
        plan_id = args[args.index("--plan-id") + 1] if "--plan-id" in args else None
        season = args[args.index("--season") + 1] if "--season" in args else None
        series = args[args.index("--series") + 1] if "--series" in args else None
        out = args[args.index("--out") + 1] if "--out" in args else None
        if not track:
            print("usage: vrs_import.py fetch [seed.har] --track 'red bull' "
                  "[--all-cars] [--series 'gt sprint'] [--season '2026 s3'] "
                  "[--out client/references.json] [--plan-id ID] "
                  "[--cookie '<Cookie header>']")
            return 2
        seed_arg = args[1] if len(args) > 1 and not args[1].startswith("--") else None
        return fetch(seed_arg, track, cookie, plan_id, "--all-cars" in args,
                     season, series, out)
    if args and args[0] == "plans":
        seed_arg = args[1] if len(args) > 1 and not args[1].startswith("--") else None
        return list_plans(seed_arg)
    if len(args) >= 2 and args[0] == "emit":
        car = args[args.index("--car-id") + 1] if "--car-id" in args else ""
        track = args[args.index("--track-id") + 1] if "--track-id" in args else ""
        return emit(Path(args[1]), car, track)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
