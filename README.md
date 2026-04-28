# RASP Cambridge — automated star-rating look-ahead

## What we found on rasp.stratus.org.uk

The "Town & City Forecasts" page drives an iframe that loads
`https://app.stratus.org.uk/blip/graph/dayview.php`, which in turn embeds
nine PNG charts. The star rating we care about comes from one specific chart:

```
https://app.stratus.org.uk/blip/graph/blip_stars.php
   ?model=<MODEL>&lat=<LAT>&lon=<LON>
```

where `<MODEL>` is one of seven codes that map to day-index 0–6:

| Day idx | Model     | Meaning                    |
|--------:|-----------|----------------------------|
| 0       | `UK4`     | Today (runs 0700–2100)     |
| 1       | `UK4+1`   | Tomorrow                   |
| 2       | `UK4+2`   | Day after tomorrow         |
| 3       | `UK12+3`  | D+3                        |
| 4       | `UK12+4`  | D+4                        |
| 5       | `UK12+5`  | D+5                        |
| 6       | `UK12+6`  | D+6                        |

Cambridge coordinates come straight from the `locn` dropdown option value
(`52.21N,0.13E` → lat=52.21, lon=0.13).

### What we tried and ruled out

- **Tabular/text endpoint** (`blip_table.php`) — returns
  "902 – Output is currently disabled by the administrator". Dead.
- **Alternate data endpoints** (`blip_data.php`, `blip_text.php`, `blip_csv.php`,
  `?format=csv|json`, `?type=data`, `?data=1`) — all 404 or redirect to the
  stratus.org.uk home page.
- **Underlying CDN** (`cdn19.mrsap.org/UK4/FCST/...`) — referenced in the
  meteogram JavaScript but returns 404 for stars/data files without
  additional auth/referer tokens.

So the PNG chart is the only exposed source. Fortunately it is fully
deterministic.

## How we extract numbers from the PNG

The chart is stable across runs:

- Fixed 710×300 size, white background
- Fixed axis position (left axis at col ~59, bottom axis at row ~241)
- Y-axis always labelled 0–6 in 7 ticks (one per integer star)
- X-axis always starts at 0700 and steps every 30 minutes
  (to 1900 for forecast days, to 2100 for today)
- The "Stars" curve is drawn in one exact RGB colour (238, 130, 238)
- Data points sit on every half-hour tick

So `rasp_stars.py`:

1. Downloads the PNG for each of the 7 model codes.
2. Finds the axis tick-mark pixels (no label OCR needed).
3. Calibrates row-to-stars linearly from the y-ticks (6 → 0).
4. For each x-tick column, takes the top magenta pixel → stars value.
5. Rounds to 0.1★ resolution.

Verified against the reference chart (25 Apr 2026): peak 2.5★ held 14:30–16:30,
mean 1.2★ — matches the attached screenshot exactly.

## Files in this folder

| File | What it is |
|------|------------|
| `rasp_stars.py` | The extractor. Takes `--location`, `--lat`, `--lon`, `--out-dir`, `--ics`. |
| `out/<date>_<model>.png` | Raw downloaded RASP chart for each day (audit trail). |
| `out/halfhour.csv` | Every half-hour rating, 7 days × 25–29 slots. |
| `out/summary.csv` | One row per day: peak, mean, hours ≥1★, ≥2★, ≥3★, peak window. |
| `cambridge_rasp.ics` | 7 all-day events — subscribe to this from MS365/iCloud. |

## Today's output (generated at runtime)

```
  2026-04-24 (Fri)     UK4   peak 1.0*  (12:30–14:00)   mean 0.4*
  2026-04-25 (Sat)   UK4+1   peak 2.5*  (14:30–16:30)   mean 1.3*   [**]
  2026-04-26 (Sun)   UK4+2   peak 3.6*  (16:00–16:30)   mean 1.4*   [***]  <-- best day
  2026-04-27 (Mon)  UK12+3   peak 2.1*  (13:00)         mean 1.0*   [**]
  2026-04-28 (Tue)  UK12+4   peak 1.4*  (12:30)         mean 0.4*
  2026-04-29 (Wed)  UK12+5   peak 1.1*  (13:00–14:00)   mean 0.3*
  2026-04-30 (Thu)  UK12+6   peak 0.1*                  mean 0.1*
```

## Scheduling and calendar delivery — recommended options

The extractor is a single self-contained script (no state, ~230 lines,
Pillow + numpy). Three delivery options, ordered by how much infrastructure
they need.

### Option A — n8n + public ICS URL (recommended)

You already run n8n. One workflow, runs daily:

1. **Cron trigger** — 05:30 UK time weekdays (and weekends).
2. **Execute Command** (or a small HTTP container running the script) —
   runs `rasp_stars.py --ics /data/cambridge_rasp.ics`.
3. **Write Binary File** — writes the ICS to a static-host location:
   - Cheapest: push to a private GitHub repo with Pages enabled, or commit to
     a public gist. Gives a permanent `https://.../cambridge_rasp.ics` URL.
   - Or: Cloudflare R2 / S3 with a public bucket + alias.
   - Or: n8n's own webhook node serving the file on GET. RASP changes twice a
     day on the back end, so a single daily refresh is plenty.
4. Subscribe to that URL from:
   - **MS365** (Outlook web) → *Add calendar → Subscribe from web* → paste URL
   - **iCloud** → Calendar.app → *File → New Calendar Subscription* → paste URL

Calendar refresh keeps the "next 7 days" view perpetually current because
each day's UID is stable (date-based), so updates replace, not duplicate.

### Option B — Cowork scheduled task (no self-hosting)

Use Cowork's `create_scheduled_task` to run the script daily. Output lands in
this workspace folder. Useful for quick daily verbal/desktop briefings —
you'd open the folder each morning rather than have a live-updating calendar.
Lower effort but no calendar subscription.

### Option C — Briefing-embedded

Hook the extractor into your existing `daily-briefing` skill so the first item
of the morning briefing is "Gliding outlook: best day in the next 7 is
Sunday 26 Apr at 3.6★ peak 16:00." No calendar, no hosting; just a line in the
briefing you already read.

All three can run side-by-side — A for the at-a-glance calendar view, C for
actionable awareness in your morning briefing.

## Caveats

- RASP model runs update through the day. The stars curve for "today" (UK4)
  refreshes more than once; fetching after ~10 am UK time gives the most
  trustworthy outlook for the afternoon.
- The parser assumes the chart template is unchanged. If Paul Scorer / the
  RASP admins change the colour, axis range, or chart size, we'd re-detect
  ticks but the line-colour constant may need updating. Keep the downloaded
  PNGs as an audit trail so any drift is visible.
- The Paul Scorer "stars" formula is experimental (the chart title says so) —
  a 3★ day won't always fly better than a 2★ day, but it's a good first-pass
  filter against washouts.
