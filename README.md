# RASP Cambridge — 7-day gliding star-rating calendar

A subscribable calendar that surfaces the next 7 days of UK
[RASP](https://rasp.stratus.org.uk/) (Regional Atmospheric Soaring Prediction)
"stars" forecast for Cambridge, regenerated daily.

Each day appears as an all-day event whose title is a single 0–5 ★ "fly score"
summarising the whole day at a glance. Open the event for the half-hour
breakdown and the original RASP chart.

```
   2026-04-26 (Sun)   3.6 ★    (best XC day this week)
   2026-04-25 (Sat)   1.2 ★
   2026-04-27 (Mon)   0.8 ★
   …
```

## Subscribe to the calendar

The calendar URL is the same in every client; the protocol prefix differs:

```
https://<your-user>.github.io/rasp-cambridge/cambridge_rasp.ics
```

**Apple Calendar / iCloud** — *File → New Calendar Subscription* → paste the
`webcal://` form of the URL.

**MS365 / Outlook web** — *Add calendar → Subscribe from web* → paste the
`https://` form.

**Google Calendar** — *Other calendars → + → From URL* → paste the `https://`
form.

### What the embedded chart looks like in each client

| Client            | Title    | Body text | Inline chart |
|-------------------|----------|-----------|--------------|
| Apple Calendar    | `2.1 ★`  | full text | yes          |
| MS365 / Outlook   | `2.1 ★`  | full text | sometimes\*  |
| Google Calendar   | `2.1 ★`  | full text | no           |

\*Outlook strips `data:` URIs in some configurations. The plain-text body
always renders, and the audit-trail PNGs are linkable from the repo's
`/charts` directory either way.

## Fork it for your own location

1. **Fork or clone this repo.**
2. **Edit `rasp_stars.py`** — change the three default constants near the top:
   ```python
   DEFAULT_LAT = 52.21
   DEFAULT_LON = 0.13
   DEFAULT_NAME = "Cambridge"
   ```
   Pick coordinates that match your launch site. You can copy the values
   straight from the `locn` dropdown on the RASP "Town & City Forecasts" page
   (e.g. `52.21N,0.13E` → `lat=52.21, lon=0.13`).
3. **Enable GitHub Pages** for your fork:
   *Settings → Pages → Source: GitHub Actions*.
4. **Run the workflow once manually** to seed the first build:
   *Actions → "Build RASP Cambridge calendar" → Run workflow*.
5. After ~90 seconds your calendar URL is
   `https://<you>.github.io/<repo>/cambridge_rasp.ics`. Subscribe to it from
   your calendar app of choice as above.

The workflow then runs daily at 11:00 UTC (catches the morning RASP run), and
the ICS in your repo refreshes itself.

## What the "fly score" means

The RASP chart gives a stars-vs-time curve through the day. A 5★ spike at 4 pm
and a long 2★ plateau both look interesting in different ways, but neither
"peak" nor "mean" alone tells you which day is XC-worthy. So the fly score
combines them:

1. **Star-hours above 2★** — the area under the curve above the XC-able
   floor, in 0.5-hour increments. 2★ is the practical minimum for any
   cross-country flight; below that you might soar locally but you're not
   getting away. Rewards both height and duration.
2. **Normalise to 0–5**, anchored so a sustained UK 5★ day (≈22 star-hours
   above 2★) maps to 5.0.

Rough interpretation:

```
5 ★   exceptional UK day - sustained 5★ core (1-2 per year)
4 ★   great XC day - 4★ plateau plus healthy shoulders (~14 sh)
3 ★   solid, committable XC (~10 sh)
2 ★   marginal XC, mostly local (~6 sh)
1 ★   brief XC-able window only (~3 sh)
0 ★   not XC-able (peak < 2★)
```

The threshold (`FLY_SCORE_THRESHOLD`, default 2.0) and anchor
(`FLY_SCORE_ANCHOR`, default 18.0) are constants near the top of the script.
If you'd rather the score reward any flyable thermal activity (e.g. for
local soaring days), drop the threshold to 1.0 and re-anchor accordingly.

## How it works under the hood

The "Town & City Forecasts" page on rasp.stratus.org.uk drives an iframe that
loads `dayview.php`, which embeds nine PNG charts. The star rating comes from
one specific chart:

```
https://app.stratus.org.uk/blip/graph/blip_stars.php
   ?model=<MODEL>&lat=<LAT>&lon=<LON>
```

where `<MODEL>` is one of seven codes mapping to day-index 0–6:

| Day idx | Model     | Meaning                |
|--------:|-----------|------------------------|
| 0       | `UK4`     | Today (runs 0700–2100) |
| 1       | `UK4+1`   | Tomorrow               |
| 2       | `UK4+2`   | Day after tomorrow     |
| 3       | `UK12+3`  | D+3                    |
| 4       | `UK12+4`  | D+4                    |
| 5       | `UK12+5`  | D+5                    |
| 6       | `UK12+6`  | D+6                    |

The number (4 vs 12) is the model grid resolution in km — UK4 is the
fine-mesh short-range model, UK12 is the coarser medium-range one.

There's no public CSV / JSON / text endpoint (`blip_table.php` returns
"902 – Output is currently disabled by the administrator"; alternate
endpoints all 404), so the PNG is the only exposed source. Fortunately
the chart template is fully deterministic:

- Fixed 710×300 size, white background
- Fixed axis position (left axis ~col 59, bottom axis ~row 241)
- Y-axis always 0–6 in 7 ticks
- X-axis starts at 0700, every 30 min (to 1900 forecast / 2100 today)
- The "Stars" curve is drawn in one exact RGB colour `(238, 130, 238)`
- Data points sit on every half-hour tick

So `rasp_stars.py`:

1. Downloads the PNG for each of the 7 model codes.
2. Auto-detects axis tick-mark pixels (no OCR needed).
3. Calibrates row→stars linearly from the y-ticks.
4. For each x-tick column, finds the top magenta pixel → stars value.
5. Rounds to 0.1★ resolution.
6. Computes the fly score and emits an ICS event with the chart embedded
   as both `ATTACH` (Apple) and `X-ALT-DESC` HTML (Outlook).

## Run it locally

```bash
git clone https://github.com/<your-user>/rasp-cambridge.git
cd rasp-cambridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python rasp_stars.py --out-dir ./out --ics cambridge_rasp.ics
```

Optional flags:

```
--location NAME      Display name (default: "Cambridge")
--lat 52.21          Latitude
--lon 0.13           Longitude
--out-dir ./out      Where audit-trail PNGs and CSVs go
--ics PATH           Where to write the calendar file
```

Output:

| File                          | What it is                                  |
|-------------------------------|---------------------------------------------|
| `out/<date>_<model>.png`      | Raw RASP chart for each day (audit trail)   |
| `out/halfhour.csv`            | Every half-hour rating, 7 days              |
| `out/summary.csv`             | One row per day with fly score and metrics  |
| `cambridge_rasp.ics`          | The calendar file                           |

## Caveats

- RASP model runs update through the day. The "today" curve (UK4) refreshes
  more than once; fetching after ~10 am UK gives the most trustworthy
  outlook for the afternoon — which is what the daily 11:00 UTC build does.
- The parser assumes the chart template is unchanged. If the RASP admins
  alter the chart colour, axis range, or size, the line-colour constant
  may need updating. The audit-trail PNGs in `/charts` make any drift
  visible.
- The Paul Scorer "stars" formula is experimental (the chart title says so) —
  a 3★ day won't always fly better than a 2★ day, but it's a good first-pass
  filter against washouts.
- Forecasts beyond D+2 (UK12 grid) are inherently coarser. Don't commit a
  cross-country task to a UK12+5 reading; do use it to spot which day in
  the back half of the week is worth checking again on the morning.

## Acknowledgements

RASP UK is run by [Paul Scorer / Stratus.org.uk](https://rasp.stratus.org.uk/)
on behalf of the UK gliding community. The "stars" formula is his work; this
project just exposes the existing forecast in a calendar-friendly form.

## Licence

GPLv3. See [LICENSE](LICENSE) for the full text. In short: fork it, modify
it, run it — but if you redistribute a modified version, do so under GPLv3
and publish your changes.
