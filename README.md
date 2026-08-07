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

The workflow then runs hourly, round the clock. Each run rebuilds unless
it fired within `MAX_AGE_MIN` (45 minutes) of the previous build — that is
a debounce against a burst of scheduler deliveries, **not** a freshness
rule. What actually gets re-fetched is decided per forecast day by the
`FRESHNESS` table in `rasp_stars.py`: today and tomorrow every run, the
back half of the week once or twice a day. A typical run therefore costs
two requests to RASP rather than seven. See
[When RASP is unavailable](#when-rasp-is-unavailable) and
[Scheduling reliability](#scheduling-reliability).

The odd minute is deliberate: GitHub delays scheduled runs under load and
drops some entirely, and the top of the hour is the worst window — see
[Scheduling reliability](#scheduling-reliability).

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

CI runs Python 3.14 (the workflow asks for `"3.14"`, so it picks up patch
releases automatically). Nothing in the code is version-specific — it has been
run unchanged on 3.11, 3.13 and 3.14 with identical output — but 3.12 went
security-only in 2026, so track something in active bugfix support.

`requirements.txt` deliberately floats (`numpy>=1.26`, `Pillow>=10.0`): each
build installs the current releases. That keeps security fixes flowing without
maintenance, at the cost of no protection against a future Pillow changing PNG
decoding under the parser. See [Caveats](#caveats).

Optional flags:

```
--location NAME      Display name (default: "Cambridge")
--lat 52.21          Latitude
--lon 0.13           Longitude
--out-dir ./out      Where audit-trail PNGs and CSVs go
--ics PATH           Where to write the calendar file
--state PATH         Where to write the JSON build-state file
```

Output:

| File                          | What it is                                  |
|-------------------------------|---------------------------------------------|
| `out/<date>_<model>.png`      | Raw RASP chart for each day (audit trail)   |
| `out/halfhour.csv`            | Every half-hour rating, 7 days              |
| `out/summary.csv`             | One row per day with fly score and metrics  |
| `cambridge_rasp.ics`          | The calendar file                           |
| `build_state.json`            | Which models were fetched (drives the next run's rebuild decision) |

### What is and isn't committed

The workflow publishes `public/` to GitHub Pages from the Actions artifact,
which is tarred from the runner's working tree — so nothing needs to be
committed for subscribers to get it. The regenerated ICS and chart PNGs are
therefore in `.gitignore`: they change wholesale on every run (base64-embedded
PNGs don't delta), and tracking them added ~500 KB of permanent history per
rebuild. They're still built, still deployed, still linkable at
`/charts/<date>_<model>.png` — just not in git.

The artifact has a second job: because it holds both the charts and
`build_state.json`, it doubles as the build's last-known-good store. See
[When RASP is unavailable](#when-rasp-is-unavailable).

`build_state.json`, the CSVs and `index.html` *are* committed: the first
because the next scheduled run has to read it, the rest because they're small
and make a usable audit trail.

## When RASP is unavailable

RASP is a volunteer-run service. It sometimes doesn't answer, sometimes
hasn't regenerated a model when the build asks for it, and sometimes sits
behind a bot-protection interstitial that returns an HTML page with a
misleading `HTTP 200`.

The governing rule is still that **a stale rating presented as current is
worse than no rating** — a 4★ reading from yesterday's model run looks
identical to a fresh one. The original implementation enforced that by
never carrying a value forward at all. That turned out to solve the right
problem the wrong way: because every run pruned, re-fetched all seven
charts and redeployed, a single failed fetch *destroyed* data that was
still perfectly good. One timeout blanked the entire week for every
subscriber until the next successful run.

The fix was to notice that "we could not fetch" and "the forecast is
unknown" are different things. The reader only needs to be able to *tell
the difference* — so every entry now states when its chart was retrieved,
and a day is blanked only once its data is genuinely obsolete.

### The freshness table

`FRESHNESS` near the top of `rasp_stars.py` is the entire policy. Two
numbers per day offset:

| Day | Model | `refresh_after` | `expire_hour` |
|-----|-------|-----------------|---------------|
| 0 (today) | UK4 | always | 03:00 UTC |
| +1 | UK4+1 | always | 03:00 UTC |
| +2 | UK4+2 | 6 h | 03:00 UTC |
| +3, +4 | UK12+3/4 | 12 h | 03:00 UTC |
| +5, +6 | UK12+5/6 | 24 h | 03:00 UTC |

`refresh_after` is how long a chart may be held before the build asks RASP
for a newer one. `expire_hour` is when it stops being publishable however
recently it was fetched.

Expiry is a **clock time rather than a duration** on purpose: a forecast is
*for a calendar day*, so what makes yesterday's chart worthless is not that
N hours elapsed, it is that the overnight model run happened. A rolling age
would let a chart fetched at 23:00 outlive one fetched at 06:00 for no good
reason. Everything held is dropped at the same daily boundary.

`refresh_after` is what varies by horizon. Today and tomorrow always chase
the latest — the UK4 "today" curve moves intra-day and tomorrow is the day
you decide on. Further out the model only regenerates about twice a day, so
a shorter interval would re-fetch identical bytes, and every request avoided
is one less chance of tripping the bot protection.

One non-obvious interaction: **expiry independently forces a fetch**, i.e.
`fetch if nothing held OR expired OR older than refresh_after`. Without
that last clause a 24-hour refresh interval combined with an 03:00 expiry
would let a chart expire before its own refresh window opened, blanking the
day for hours while it waited for permission to try.

### Three states, not two

| State | Meaning | Published? |
|-------|---------|------------|
| `ok` | fetched this run | yes |
| `cached` | held, still inside its refresh window — normal | yes |
| `carried` | held because the refresh *failed* — degraded but usable | yes |
| *(expired)* | past `expire_hour` with no success since | no — placeholder |

Anything not fetched this run says so on the entry:

```
Chart retrieved 2026-08-06 14:00 UTC
Not refreshed since - 1.0h old as of 15:00 UTC
From the UK12+4 run - UK4+1 not yet available
```

That third line matters. A date walks UK12+6 → … → UK4 as it approaches, so
a fallback chart can come from a coarser model than the slot wants. Showing
12 km data in a 4 km slot without saying so is exactly what the original
rule existed to prevent.

Only once a day passes `expire_hour` with no successful fetch is it blanked:

```
   ⟳ RASP data unavailable
```

Opening it explains that there is no forecast for that day and that a retry
is coming. A later run replaces it (same event UID) as soon as RASP serves
that chart again. Why the fetch failed — unreachable, not yet regenerated,
intercepted, unparseable — is operational detail that stays in the Actions
log and `build_state.json`, never in a calendar entry.

### Where the fallback comes from

There is no cache to configure. The Pages artifact already contains
`build_state.json` *and* every chart PNG, so **the published site is the
last-known-good store** — the workflow passes its own site URL as
`--previous-base`, and the build pulls back exactly what subscribers are
currently being served. Nothing to evict, nothing to expire out from under
you, and a cold start (no site yet) simply means no fallback that run.

Transient transport errors are retried twice with backoff. Interception is
*not* retried: each Actions job holds one IP, so if that IP is being
challenged, retrying from the same runner is challenged too. A fresh runner
on the next slot is the effective retry.

Each run writes `public/build_state.json` recording which of the seven models
came back:

```json
{ "generated_utc": "2026-08-07T06:00:04Z", "run_date": "2026-08-07",
  "complete": true, "days_ok": 7, "days_fresh": 2, "days_held": 5,
  "days_failed": 0,
  "days": [
    { "date": "2026-08-07", "model": "UK4",    "source_model": "UK4",
      "status": "ok",     "fetched_utc": "2026-08-07T06:00:04Z" },
    { "date": "2026-08-09", "model": "UK4+2",  "source_model": "UK4+2",
      "status": "cached", "fetched_utc": "2026-08-07T04:14:11Z" }
  ] }
```

This file has two readers. The workflow uses the top-level fields to decide
whether to build at all. The *next run* uses the per-day `fetched_utc` and
`source_model` to find and age the charts it already holds — which is why
it is published alongside the charts rather than kept only in git.

`days_ok` counts everything publishable however obtained; `days_fresh` is
what actually came from RASP this run. A healthy steady state looks like
`days_fresh: 2, days_held: 5` — today and tomorrow refreshed, the rest
inside their windows.

## Scheduling reliability

GitHub Actions cron is best-effort, not a guarantee. Scheduled runs are queued
on shared infrastructure, delayed under load, and **dropped entirely** when
load is high enough — and the top of every hour is the worst window, because
that is when most of the world's cron entries fire.

Measured here on 2026-07-29, with every cron still at `:00`:

| Scheduled (UTC) | Actually ran | Delay |
|-----------------|--------------|-------|
| 10:00 | 10:26 | +26 min |
| 11:00 | 11:51 | +51 min |
| 12:00 | — | dropped |
| 13:00 | 13:01 | +1 min |
| 14:00 | — | dropped |
| 15:00 | 15:49 | +49 min |
| 16:00 | — | dropped |
| 17:00 | 17:16 | +16 min |

Three of eight slots never fired. Moving to an unpopular minute helps less
than you'd hope: re-measured at `:23` over three days, the loss rate was
still ~30%, one dispatch ran +74 minutes late, and the pre-dawn slots never
fired at all — a blackout that persisted at `:13` too. The delay pattern
also repeats day to day within a few minutes, so it tracks global cron load
rather than random jitter. The minute has been through `:00` → `:23` →
`:13` → `:45` (current); none measurably beat the others, so any minute
choice is a cheap experiment, not a fix. The schedule now covers all 24
hours, because more pre-dawn slots are the only cheap way to get a chance
at an early-bird build — an overnight slot with nothing to do costs about
ten seconds.

What actually makes the schedule reliable is treating it as lossy: no
single firing matters. Every slot re-runs the same decision, so a dropped
one defers to the next survivor, and any run that finds a gap closes it.
There is deliberately no distinction between "anchor" and "recheck" runs:
an earlier design with unconditional anchor crons plus conditional hourly
rechecks left a dropped anchor uncompensated (the next recheck saw complete
state and skipped, so the "today" curve stayed stale), and told the two
apart by parsing the cron string, which made the schedule fragile to edit.

`MAX_AGE_MIN` is now only a **debounce** — a floor on the gap between
builds, currently 45 minutes, well under the smallest gap ever observed
between real runs (136 min), so in practice it never binds. It exists for
the day GitHub delivers all 24 slots at once.

It used to be the freshness policy, and that is worth recording because the
failure was silent. A single global age threshold is a **floor, not an
interval**: the real refresh period is the threshold plus the wait for the
next slot that actually arrives. At 180 minutes it sat just *above* the
136–180 minute gap between arrivals and skipped alternate surviving runs,
turning a nominal 3-hour policy into a ~5-hour one with nothing failing to
show it. The deeper problem is that one number cannot express "today
matters more than next Tuesday" — which is why freshness now lives in the
per-horizon `FRESHNESS` table instead. Don't grow this one back.

If a firing ever becomes genuinely load-bearing, don't lean harder on GitHub's
scheduler — trigger `workflow_dispatch` from an external scheduler via the
API, which is not subject to the same queue.

### Action pinning

Actions are pinned to full commit SHAs, with the version in a trailing
comment. Tags are mutable — `@v7` can be force-moved to any commit by whoever
controls that repo, and these actions execute with this workflow's token and
`contents: write`. `.github/dependabot.yml` raises a weekly PR when a pin falls
behind; without it, SHA pinning just means running whatever was current on the
day you pinned, forever.

Permissions are scoped per job rather than workflow-wide: only `build` gets
`contents: write` (it commits `build_state.json`), and `deploy` gets
`pages`/`id-token` and nothing else.

Two other things silently stop scheduled workflows, worth knowing before
debugging a quiet morning:

- **60 days of repository inactivity** disables them automatically (a banner
  appears in the Actions tab). Not a risk while the build commits
  `build_state.json` on every rebuild, which is one reason it stays tracked.
- Schedules are only read from the **default branch**, so a cron change on a
  side branch does nothing until it lands on `main`.

## Caveats

- RASP model runs update through the day. The "today" curve (UK4) refreshes
  more than once; fetching after ~10 am UK gives the most trustworthy
  outlook for the afternoon. Today and tomorrow are re-fetched on every
  surviving slot, so the late-morning and afternoon reruns are picked up as
  a matter of course.
- Expect a burst of seven requests on the first run after 03:00 UTC each
  day, when everything held expires at once, then two per run for the rest
  of the day. That is the intended shape, not a fault.
- The parser assumes the chart template is unchanged. If the RASP admins
  alter the chart colour, axis range, or size, the line-colour constant
  may need updating. The audit-trail PNGs in `/charts` make any drift
  visible.
- Dependencies float, and there are no tests. The whole program is
  pixel-archaeology — an exact RGB match on `(238,130,238)`, tick detection
  by counting dark pixels — so a future Pillow that changes PNG decoding
  could shift star ratings with nothing failing. A committed reference PNG
  plus its expected half-hour readings, asserted in CI, would close both
  this and the chart-template risk above.
- The Paul Scorer "stars" formula is experimental (the chart title says so) —
  a 3★ day won't always fly better than a 2★ day, but it's a good first-pass
  filter against washouts.
- Forecasts beyond D+2 (UK12 grid) are inherently coarser. Don't commit a
  cross-country task to a UK12+5 reading; do use it to spot which day in
  the back half of the week is worth checking again on the morning.

## Project documentation

Contributor-facing documentation lives in [`docs/`](docs/index.md) —
architecture, publishing model, scheduling design and measurements,
troubleshooting, plus filesystem and dependency maps. AI agents (and
humans in a hurry) should start at [`AI_README.md`](AI_README.md), which
holds the rules that must not be broken.

## Acknowledgements

RASP UK is run by [Paul Scorer / Stratus.org.uk](https://rasp.stratus.org.uk/)
on behalf of the UK gliding community. The "stars" formula is his work; this
project just exposes the existing forecast in a calendar-friendly form.

## Licence

GPLv3. See [LICENSE](LICENSE) for the full text. In short: fork it, modify
it, run it — but if you redistribute a modified version, do so under GPLv3
and publish your changes.
