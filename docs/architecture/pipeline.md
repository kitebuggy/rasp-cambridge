# Pipeline architecture

## Overview

`rasp_stars.py` turns seven RASP "stars" forecast chart PNGs into one ICS
calendar. Everything happens in a single pass per run: fetch each day's
chart, parse the pixels into half-hourly star values, summarise each day
into a 0-5 fly score, and write the CSVs, the ICS and the build-state file.
A day whose fetch fails falls back to the chart already published for it,
labelled with its retrieval time; only once that chart passes its expiry
does the day become a placeholder.

## Data flow

```
                        FRESHNESS[offset] → (refresh_after, expire_hour)
                                 │
   held chart due a refresh, expired, or absent?  ──no──► reuse it ("cached")
                                 │yes                          │
                                 ▼                             │
app.stratus.org.uk/blip/graph/blip_stars.php  (only the days that need it)
        │  PNG bytes                                           │
        ▼                                                      │
fetch_png ── Last-Modified < today? ──► StaleChartError ─┐     │
        │    not a PNG? ──► NotAChartError ──────────────┤     │
        │    timeout/URLError ──► 2 retries, backoff ────┤     │
        ▼                                                ▼     │
        │                                    held chart still in date?
        │                                          │yes    │no │
        │                             reuse it ("carried") │   │
        ▼                                          │       ▼   │
parse_stars_png (tick detection, pixel scan) ◄─────┴───────┼───┘
        │  [(HH:MM, stars)] per day     parse error ───────┤
        ▼                                                  ▼
summarise ──► DaySummary (+ fetched_utc,          DayFailure (per blank day)
        │      source_model, origin)                       │
        ▼                                                  ▼
write_csv / write_summary_csv        write_ics (real events + placeholders,
        │                                       shared UID scheme)
        ▼                                                  │
write_state ──► build_state.json  ◄────────────────────────┘
                │
                └─► published to Pages, and read back next run via
                    --previous-base (load_published_state /
                    fetch_published_chart) as the last-known-good store
```

## The data source

The RASP "Town & City Forecasts" page embeds nine PNG charts via
`dayview.php`; the stars rating comes from one of them:

```
https://app.stratus.org.uk/blip/graph/blip_stars.php?model=<MODEL>&lat=<LAT>&lon=<LON>
```

`DAY_MODELS` maps day index 0-6 to model codes `UK4`, `UK4+1`, `UK4+2`,
`UK12+3`..`UK12+6` (taken directly from the RASP page's JS). UK4 is the
4 km short-range model, UK12 the 12 km medium-range one. There is no
CSV/JSON endpoint (`blip_table.php` is administratively disabled), so the
PNG is the only source.

Two important facts about `blip_stars.php`, verified 2026-07-29:

- It has **no HTML error path** - every input tried (bogus model, nonsense
  lat/lon, no params) returns a PNG. An HTML response therefore means
  something *in front of* RASP answered (WAF/interstitial) - see
  `../operations/troubleshooting.md`.
- Its error-ish outputs are **small valid PNGs**, and all of them are
  rejected by the tick-count guard (below) rather than parsing into a bogus
  rating.

## Two different kinds of "old data"

Do not conflate these - the first is rejected, the second is published.

**RASP serving yesterday's file** (`StaleChartError`). RASP keeps serving
the previous run's chart until it regenerates a model, and that file has
yesterday's date stamped *inside* it. `fetch_png` parses the
`Last-Modified` header and rejects anything whose UTC date predates today.
Never weaken this guard: the chart is mislabelled at source, so we cannot
say anything truthful about it.

**A chart we fetched ourselves earlier** (carry-forward). This one we know
the provenance of exactly, so it can be republished with its retrieval time
on the entry. Governed by `FRESHNESS`, keyed by day offset:

| Day | Model | `refresh_after` | `expire_hour` |
|-----|-------|-----------------|---------------|
| 0 (today) | UK4 | always | 03:00 UTC |
| +1 | UK4+1 | always | 03:00 UTC |
| +2 | UK4+2 | 6 h | 03:00 UTC |
| +3, +4 | UK12+3/4 | 12 h | 03:00 UTC |
| +5, +6 | UK12+5/6 | 24 h | 03:00 UTC |

`refresh_after` is when to ask RASP again; `expire_hour` is when a held
chart stops being publishable regardless. Expiry is a **clock time, not a
duration**, because what invalidates a chart is the overnight model run,
not elapsed hours - and a rolling age would let a chart fetched at 23:00
outlive one fetched at 06:00 for no reason.

`want_fetch = nothing held OR expired OR age >= refresh_after`. The
`expired` clause is load-bearing: without it a 24 h refresh interval could
let a chart expire before its own refresh window opened, blanking the day
for hours.

Three publishable states result - `ok` (fetched this run), `cached` (held,
inside its refresh window), `carried` (held because the refresh failed) -
and all three are read back as usable by the next run. Non-fresh entries
carry provenance lines: retrieval time, current age, and - if the held
chart came from a coarser model than the slot wants, since a date walks
UK12+6 → … → UK4 as it approaches - which model produced it.

## Chart parsing (parse_stars_png, _find_ticks)

The chart template is deterministic: 710×300, white background, left axis
~col 59, bottom axis ~row 241, y-axis 0-6 in 7 ticks, x-axis from 07:00 in
30-minute steps, and the stars curve drawn in exactly `LINE_RGB =
(238, 130, 238)`.

- `_find_ticks` finds the bottom axis (row with most dark pixels) and left
  axis (column with most dark pixels), then collects tick marks adjacent to
  them, deduplicating within 3 px.
- Guards: x-tick count must be within `N_SLOTS_MIN..N_SLOTS_MAX` (12..32) -
  this is what rejects RASP's small error charts - and at least 2 y-ticks
  must be found.
- Row→stars is calibrated linearly from the y-ticks (top tick = 6, bottom =
  0). For each x-tick, the topmost `LINE_RGB` pixel within ±3 columns gives
  the star value (median across columns), rounded to 0.1★.
- Slots are timestamped from `HOUR_START` (07:00) at `SLOT_MIN` (30-minute)
  cadence; tick count varies by model (29 for today's UK4 to ~23 for
  truncated long-range days) and is accepted anywhere in the plausible
  range.

If RASP ever changes the chart template (colour, size, axis range), it is
`LINE_RGB` and the geometry assumptions here that break. The audit-trail
PNGs under `public/charts/` make drift visible.

## Day summarisation and the fly score

`summarise` computes per-day metrics (peak, mean, hours ≥1★/≥2★/≥3★, XC
window = first..last slot ≥2★) and the headline **fly score**:

1. Integrate star-hours above `FLY_SCORE_THRESHOLD` (2.0★ - the practical
   XC floor), each slot worth 0.5 h.
2. Normalise to 0-5 via `FLY_SCORE_ANCHOR` (18.0 star-hours ↦ 5.0), capped
   at 5.

The rationale and calibration (a realistic UK 5★ day ≈ 22 star-hours above
2★) are in the long comment above the constants in `rasp_stars.py`, and in
the README's "What the fly score means" section.

## ICS generation (write_ics)

- One all-day `VEVENT` per day; `SUMMARY` is just `<fly_score> ★`.
- `UID` is `rasp-<location>-<yyyymmdd>@rasp-cambridge` - date-based, so any
  later run's event for the same day **replaces** the earlier one on
  refresh. Placeholders share the same scheme, which is what lets a
  successful retry supersede a placeholder (and vice versa).
- Each real event carries the chart three ways: plain-text `DESCRIPTION`
  (all clients, incl. Google, with a Unicode sparkline from `_spark`), an
  `ATTACH` base64 PNG (Apple), and an `X-ALT-DESC` HTML body with a
  half-hour table and inline `data:` image (Apple/Outlook; Outlook
  sometimes strips `data:` URIs).
- All content lines are folded at 75 octets per RFC 5545 §3.1 by
  `_fold_ics_line`, which is octet-based and keeps splits on UTF-8
  character boundaries (the ★ and sparkline glyphs are multi-byte).

## Placeholders (DayFailure)

A day only reaches `DayFailure` once nothing publishable is left: the fetch
failed (or was never attempted) *and* the held chart is past `expire_hour`,
or there was never a held chart. `kind` is `stale` / `intercepted` /
`fetch` / `parse` / `expired`, with a human-readable `reason`. The
placeholder event renders **neither** kind nor reason - it
says only that no forecast is available, that blank-on-purpose beats stale
data, and that a retry is coming (`RETRY_NOTE_DEFAULT`, deliberately vague
about times because the scheduler is lossy). The diagnostics go to stderr
and into `build_state.json`. This split is AI_README Critical rule 2.

## Build state (write_state)

`build_state.json` records `generated_utc`, `run_date`, `complete`,
`days_ok` (publishable however obtained), `days_fresh` (actually fetched
this run), `days_held`, `days_failed`, and a per-day list carrying
`status`, `source_model` and `fetched_utc`.

It has **two readers**. The workflow's decide step uses the top-level
fields to choose rebuild vs no-op (see `../operations/scheduling.md`) -
which is why it is committed to git. The *next run* uses the per-day
`fetched_utc` / `source_model` to find and age the charts it holds, reading
them back off the published site rather than from git - which is why it is
also deployed alongside the charts.

A healthy steady state is `days_fresh: 2, days_held: 5`: today and tomorrow
refreshed, the rest inside their windows.

## Exit-code contract

`rasp_stars.py` exits 0 even when days failed: placeholders are a valid,
publishable result and the build must still deploy them. Completeness is
signalled through the state file, not the exit code. Do not "fix" this by
exiting non-zero on failures - that would stop the placeholder calendar
from deploying.
