# VRS importer — fill `references.json` from your VRS datapacks

`vrs_import.py` turns the fuel data inside your Virtual Racing School
datapacks into ready-to-paste rows for `client/references.json` — real
per-car, per-track burn rates, lap times, and BoP tank sizes, instead of
hand-typed guesses.

It works with **your own VRS login** (you need a subscription that includes
the datapacks). Nothing is scraped from pages: the tool talks to the same
internal API the VRS web app itself uses, authenticated as you.

---

## One-time setup (per season)

The tool needs two things it can only learn from your browser once: how the
VRS app talks to its server, and your login cookie. Both come from a single
"HAR capture" — a file your browser can export that records network traffic.

1. Open your browser and log in to VRS.
2. Press **F12** (devtools) → **Network** tab → tick **Preserve log**
   (Chrome/Edge/Firefox all have it).
3. In VRS, open your datapack and click one track so the **lap list with
   fuel numbers** is on screen.
4. Right-click anywhere in the devtools request list →
   **"Save all as HAR (with content)"** → save as `capture.har` into the
   `vrs_bodies/` folder of this repo.
5. Run the first fetch **with** the HAR:

   ```
   python tools/vrs_import.py fetch vrs_bodies/capture.har --track "red bull"
   ```

That run prints your first reference row AND saves everything reusable to
`vrs_bodies/vrs_seed.json`. **You never need the HAR again** — you can
delete it after this step.

> `vrs_bodies/` and `*.har` are git-ignored on purpose: both the HAR and
> the seed file contain your VRS session cookie. Never commit them, never
> share them.

---

## Everyday use

```
python tools/vrs_import.py fetch --track "spa"
```

- `--track` takes the track name **as the VRS UI shows it** — any
  unambiguous part works: `"red bull"`, `"24 heures"`, `"watkins"`.
  If the name is ambiguous or unknown, the tool prints the season's track
  list so you can pick.
- Without `--out`, the rows are only **printed** — paste them into
  `client/references.json` yourself (inside the `[ ... ]` list,
  comma-separated like the other rows).
- With `--out client/references-vrs.json`, the rows are also written to a
  **new timestamped file** — `references-vrs-20260823-1511.json` — so
  nothing existing is ever touched and every run keeps its own snapshot.
  Combine into your live `references.json` yourself, or point the client's
  `--refs` straight at the snapshot. Rows with an unresolved id
  (`FILL_ME`/`VERIFY:`) are written but flagged — fix the id or the client
  will never match that row.
- Fields starting with `_` are comments — the `_source` line records where
  the numbers came from.

What the tool does for you on each run:

- finds the track's sessions in the datapack,
- **skips sessions labeled wet or pre-BoP**,
- reads every lap's time and fuel level,
- computes the median lap time and median fuel burn per lap,
- reads the **BoP-effective tank size** straight from the data,
- maps the car and track to iRacing's internal ids where known
  (a `VERIFY:` prefix or `FILL_ME` means: check that id yourself — the
  PitWall client's startup warning always quotes the correct string).

A track can come back with `sessions: 0` — that simply means its race week
hasn't happened yet this season. Try again after that week.

---

## All cars in the series (one row per car)

Competitor prediction needs a row for every car model in your splits, not
just yours. Each car has its own VRS datapack, and the tool can loop over
all of them — it just has to learn their ids once:

1. Same HAR recipe as above, but this time capture the VRS page that
   **lists the series' datapacks per car** (the picker where you'd switch
   from the Mercedes pack to the Ferrari one). Save as
   `vrs_bodies/picker.har`.
2. Harvest it once (any fetch with the HAR merges it into the seed):

   ```
   python tools/vrs_import.py fetch vrs_bodies/picker.har --track "red bull" --all-cars
   ```

3. From then on, no HAR:

   ```
   python tools/vrs_import.py plans                # see which datapacks it knows
   python tools/vrs_import.py fetch --track "spa" --all-cars \
       --series "gt sprint" --season "2026 s3" --out client/references-vrs.json
   ```

`--all-cars` prints one reference row per car's datapack, each with that
car's own tank and burn. Two filters keep the loop honest:

- `--series` — any part of the datapack name (`"gt sprint"`). Without it,
  every datapack in the seed is included, other series too.
- `--season` — **always mind the season.** BoP (tank sizes!) and the track
  calendar change between seasons, so rows from different seasons must
  never be mixed. Accepts `"2026 s3"`, `"2026 season 3"`, or any part of
  the datapack name. If you leave it off, the tool uses the newest season
  among the matching datapacks and tells you so.

A filter that matches nothing prints the full list of known datapacks, so
you can see what to type.

---

## When the login expires

Sooner or later a run will stop with *"VRS rejected the call (session
cookie probably expired)"*. **Don't re-capture a HAR.** Instead:

1. In a logged-in VRS browser tab: F12 → Network → click any request →
   **Request Headers** → copy the whole value of the `Cookie` header.
2. Run once with it:

   ```
   python tools/vrs_import.py fetch --track "spa" --cookie '<paste here>'
   ```

   (or `export VRS_COOKIE='<paste>'` and run normally.)

The new cookie is saved into the seed file; subsequent runs need nothing.

---

## Reading the numbers (what to trust)

| Field | Where it comes from | Trust |
|---|---|---|
| `tank_capacity_l` | fuel level ÷ tank fraction, from the data itself | high — this is the BoP-effective tank |
| `baseline_lap_time_s` | median lap of the datapack sessions | pro pace: faster than your race pace (fine — the engine anchors on each car's own live pace) |
| `baseline_burn_l_per_lap` | median lap-to-lap fuel drop | slightly push-side (pro pace). Safe direction for this engine — it errs toward pitting early |
| `push/save_burn_l_per_lap` | ±% around baseline | rough; refine from your own logging if you care |
| `refuel_rate_l_per_s`, `tyre_change_time_s`, `fixed_pit_overhead_s` | fixed GT3 defaults, **not** from VRS | validate against real logged stops |

---

## Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `no saved seed at ...` | first run ever — do the one-time HAR setup above |
| `seed is missing [...]` | the HAR was captured without the lap view open; re-capture with a track's fuel/lap table on screen |
| `VRS rejected the call` | cookie expired — paste a fresh one (see above) |
| `sessions: 0 total` | that track's race week hasn't run yet this season |
| `matched 0 of these tracks` | name typo, or wrong datapack/season — the printed list shows what exists |
| `no datapack plan list in the seed yet` | `--all-cars` needs the picker capture (see "All cars") |
| `0 datapack plan(s) known` after harvesting the picker HAR | the picker response looks different than expected — keep the HAR in `vrs_bodies/` and ask Claude to adapt the parser |
| A `VERIFY:`/`FILL_ME` track or car id | join any iRacing session with the PitWall client; its warning quotes the exact string to paste |

Other subcommands (rarely needed): `explore <har>` dumps every VRS response
in a capture to `vrs_bodies/*.json` for inspection; `emit <har> --car-id X
--track-id Y` builds rows offline from a capture without calling VRS.

---

## Be a good citizen

- One capture, few requests: the tool throttles itself (2 requests/second
  max) and only fetches what you ask for.
- This uses VRS's **internal, undocumented** API with your personal login.
  It reads only data your subscription already shows you — keep it that
  way: personal use, no bulk downloads, no sharing the output publicly.
- A VRS app update may break the tool at any time. Nothing bad happens —
  runs just start failing — but expect to need a fresh capture and possibly
  a code fix when they ship changes.
