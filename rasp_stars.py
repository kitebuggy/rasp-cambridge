#!/usr/bin/env python3
# rasp_stars.py - extract RASP UK 'Stars' forecast values from PNG charts
#                 and emit a subscribable ICS calendar.
#
# Copyright (C) 2026  Jason Holloway and contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
Fetch RASP UK 'Stars' forecast chart for a location (default: Cambridge)
and extract numeric values by parsing the PNG pixels.

Output:
  - CSV of half-hourly star ratings for each of the 7 forecast days
  - Optional ICS calendar file with one all-day event per day containing
    a fly-score summary, a Unicode sparkline, and the original chart.

Usage:
  python3 rasp_stars.py --out-dir ./out --ics cambridge_rasp.ics
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import io
import json
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image

# -----------------------------------------------------------------------------
# Configuration

BASE = "https://app.stratus.org.uk/blip/graph/blip_stars.php"

# Day index 0..6 -> model code. Taken directly from the RASP JS on the
# town-and-city-forecasts page.
DAY_MODELS = ["UK4", "UK4+1", "UK4+2", "UK12+3", "UK12+4", "UK12+5", "UK12+6"]

# -----------------------------------------------------------------------------
# Freshness policy - THE table.  Tune here; nothing else decides staleness.
#
# Keyed by day offset from today.  Two numbers per horizon:
#
#   refresh_after   minutes we may keep the chart we already hold before
#                   asking RASP for a newer one.  0 = try every run.
#   expire_hour     the wall-clock hour (UTC) at which a held chart stops
#                   being publishable, however recently it was fetched.
#
# Why two, and why the second is a CLOCK time rather than a duration: a
# forecast is *for a calendar day*, so what makes yesterday's chart
# worthless is not that N hours elapsed, it is that the overnight model
# run happened.  Everything held is therefore dropped at the same daily
# boundary, and a rolling age would let a chart fetched at 23:00 outlive
# one fetched at 06:00 for no good reason.
#
# 03:00 UTC (04:00 BST) - after the overnight regen, before anyone looks
# at the day.  Same for every horizon today, but it is a per-row column
# so a horizon can be given its own boundary without touching the logic.
#
# refresh_after is what differs by horizon.  Today and tomorrow always
# chase the latest: the UK4 "today" curve moves intra-day and tomorrow is
# the day you decide on.  Further out the model only regenerates about
# twice a day, so a shorter interval re-fetches identical bytes - and
# every request avoided is one less chance of tripping the bot protection
# sitting in front of RASP.
#
# Between refresh_after and expiry the chart is still published, with its
# true retrieval time stated on the entry.  That is the point: "we could
# not fetch" and "the forecast is unknown" are different, and only the
# second one deserves a blank.  Nobody reads an old rating as a current
# one, which is what the never-carry-stale-data rule was protecting.
#
#                    refresh_after   expire_hour_utc
FRESHNESS: dict[int, tuple[int, int]] = {
    0:                (0,            3),   # today    - always refresh
    1:                (0,            3),   # tomorrow - always refresh
    2:                (6 * 60,       3),
    3:                (12 * 60,      3),
    4:                (12 * 60,      3),
    5:                (24 * 60,      3),   # +5 / +6  - a day is ample here
    6:                (24 * 60,      3),
}
FRESHNESS_DEFAULT = (0, 3)

# Network behaviour.  Retries help a TIMEOUT and do nothing for a bot
# challenge: each Actions job holds one IP, so if that IP is challenged,
# retrying from the same runner is challenged too.  Only transient
# transport errors are retried - an intercept page raises NotAChartError
# outside the loop and fails immediately.
FETCH_TIMEOUT = 30
FETCH_RETRIES = 2
FETCH_BACKOFF = 3.0

# Default location: Cambridge (from locn dropdown value '52.21N,0.13E')
DEFAULT_LAT = 52.21
DEFAULT_LON = 0.13
DEFAULT_NAME = "Cambridge"

# RASP charts always start at 0700 and step every 30 min.  The chart end
# time varies by model:
#   today (UK4)            29 ticks  =>  0700..2100
#   short-range (UK4+N)    25 ticks  =>  0700..1900
#   medium-range (UK12+N)  23-25 ticks; longer-range days are sometimes
#                          truncated to 0700..1800 (23 ticks) when the
#                          model run for that day hasn't fully populated.
# We accept any tick count in the plausible range and time-stamp slots
# from HOUR_START at SLOT_MIN-minute cadence.
HOUR_START = 7
SLOT_MIN   = 30
N_SLOTS_MIN = 12   # below this it's almost certainly a corrupt chart
N_SLOTS_MAX = 32   # leaves headroom if the chart ever extends to 2200

# The stars curve is drawn as solid "violet" (238,130,238) PNG pixels.
LINE_RGB = (238, 130, 238)

# Shown in placeholder calendar entries.  Deliberately vague about exact
# times: GitHub delays and sometimes drops scheduled runs, so quoting a
# precise timetable to the reader would be a promise the scheduler doesn't
# keep - and would need editing every time the cron in
# .github/workflows/build-rasp.yml moves.
RETRY_NOTE_DEFAULT = ("The build retries roughly hourly through the day, "
                      "and this entry is replaced automatically as soon as "
                      "the forecast is available again.")


# -----------------------------------------------------------------------------
# Helpers

class StaleChartError(RuntimeError):
    """Raised when RASP serves a chart from a previous day's model run."""


class NotAChartError(RuntimeError):
    """Raised when the response isn't a PNG at all (usually an intercept page)."""


def _describe_non_png(data: bytes, status: int, headers, url: str) -> str:
    """
    Summarise a non-PNG response so a failure is diagnosable after the fact.

    blip_stars.php returns a PNG for every input tested, including bogus
    models and nonsense coordinates - it has no HTML error path.  So an
    HTML body means something in front of RASP answered instead: a WAF,
    bot-protection interstitial or host error page, typically with a
    misleading HTTP 200.  Knowing WHICH decides what to do about it, and
    that information is only available at the moment it happens - hence
    capturing it here rather than just the first 16 bytes.

    The full body goes to stderr (visible in the Actions log); the short
    form returned here goes into build_state.json and the calendar
    placeholder, so it has to stay readable.
    """
    text = data.decode("utf-8", "replace")
    # <title> is usually the most identifying single line on a block page.
    title = ""
    lower = text.lower()
    i = lower.find("<title>")
    if i != -1:
        j = lower.find("</title>", i)
        if j != -1:
            title = " ".join(text[i + 7:j].split())[:120]
    # Crude tag strip for a sample of the visible text.
    stripped, in_tag = [], False
    for ch in text[:4000]:
        if ch == "<":
            in_tag = True
        elif ch == ">":
            in_tag = False
        elif not in_tag:
            stripped.append(ch)
    visible = " ".join("".join(stripped).split())[:200]

    print(f"    --- non-PNG response from {url}", file=sys.stderr)
    print(f"    HTTP {status}, Content-Type "
          f"{headers.get('Content-Type', '?')}, Server "
          f"{headers.get('Server', '?')}, {len(data)} bytes", file=sys.stderr)
    print(f"    title: {title or '(none)'}", file=sys.stderr)
    print(f"    body:  {text[:1200]}", file=sys.stderr)
    print("    --- end of non-PNG response", file=sys.stderr)

    bits = [f"HTTP {status}",
            f"content-type {headers.get('Content-Type', '?')}",
            f"{len(data)} bytes"]
    if title:
        bits.append(f'title "{title}"')
    if visible:
        bits.append(f'text "{visible}"')
    return "not a PNG - " + "; ".join(bits)


def fetch_png(model: str, lat: float, lon: float) -> bytes:
    """
    Download one RASP chart PNG.  Rejects stale charts by inspecting the
    server's Last-Modified header: if RASP hasn't yet regenerated today's
    forecast for this model, it serves yesterday's file (with yesterday's
    declared date inside the PNG).  We refuse to use such files - the next
    scheduled workflow run will retry once RASP has caught up.
    """
    params = {
        "model": model,
        "lat": f"{lat:.5f}",
        "lon": f"{lon:.5f}",
    }
    url = f"{BASE}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "rasp-stars-poller/1.0"})
    last_exc: Exception | None = None
    for attempt in range(1 + FETCH_RETRIES):
        if attempt:
            time.sleep(FETCH_BACKOFF * attempt)
            print(f"    retry {attempt}/{FETCH_RETRIES} after {last_exc}",
                  file=sys.stderr)
        try:
            with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                data = resp.read()
                lm_header = resp.headers.get("Last-Modified", "")
                status = resp.status
                headers = resp.headers
            break
        except (TimeoutError, socket.timeout, URLError) as e:
            last_exc = e
    else:
        raise TimeoutError(
            f"{1 + FETCH_RETRIES} attempts failed; last: {last_exc}")
    if not data.startswith(b"\x89PNG"):
        raise NotAChartError(_describe_non_png(data, status, headers, url))
    # Compare Last-Modified day (UTC) with today (UTC).  If older than today,
    # the chart is from a previous model run and almost certainly has the
    # wrong calendar date stamped in its title.
    if lm_header:
        try:
            # RFC 7231 IMF-fixdate; eg "Wed, 13 May 2026 10:22:36 GMT".
            lm = dt.datetime.strptime(lm_header.rstrip("GMT").strip(),
                                      "%a, %d %b %Y %H:%M:%S")
            lm = lm.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            lm = None
        if lm is not None:
            today_utc = dt.datetime.now(dt.timezone.utc).date()
            if lm.date() < today_utc:
                raise StaleChartError(
                    f"chart Last-Modified {lm.date()} is from before "
                    f"today ({today_utc}); RASP hasn't refreshed this "
                    f"model yet"
                )
    return data


def _find_ticks(arr: np.ndarray) -> tuple[list[int], list[int]]:
    """Return (x_tick_cols, y_tick_rows) detected from axis tick marks."""
    H, W, _ = arr.shape
    dark = (arr[:, :, 0] < 80) & (arr[:, :, 1] < 80) & (arr[:, :, 2] < 80)

    # X ticks live immediately below the bottom axis (row ~241).  Find the
    # bottom axis row first: the row with the most dark pixels.
    bottom_row = int(np.argmax(dark.sum(axis=1)))
    # Tick marks: columns where the two rows below the axis are dark.
    x_ticks = [
        c for c in range(W)
        if dark[bottom_row + 1, c] and dark[bottom_row + 2, c]
    ]
    # Dedupe adjacent pixels.
    dedup_x: list[int] = []
    for c in x_ticks:
        if not dedup_x or c - dedup_x[-1] > 3:
            dedup_x.append(c)

    # Y ticks live just left of the left axis (col ~59).  Find the axis column.
    left_col = int(np.argmax(dark.sum(axis=0)))
    y_ticks = [
        r for r in range(H)
        if dark[r, left_col - 2] and dark[r, left_col - 3]
    ]
    dedup_y: list[int] = []
    for r in y_ticks:
        if not dedup_y or r - dedup_y[-1] > 3:
            dedup_y.append(r)
    # RASP 'Stars' plots always run 6 -> 0 top-to-bottom in 7 labelled steps.
    dedup_y = dedup_y[:7]
    return dedup_x, dedup_y


def parse_stars_png(data: bytes) -> list[tuple[str, float]]:
    """Extract (HH:MM, stars) pairs from one RASP Stars PNG."""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    arr = np.array(im)
    line_mask = np.all(arr == np.array(LINE_RGB), axis=2)

    x_ticks, y_ticks = _find_ticks(arr)
    if not (N_SLOTS_MIN <= len(x_ticks) <= N_SLOTS_MAX):
        raise RuntimeError(
            f"Implausible x-tick count {len(x_ticks)} "
            f"(expected {N_SLOTS_MIN}..{N_SLOTS_MAX})"
        )
    if len(y_ticks) < 2:
        raise RuntimeError(f"Could not detect y-axis ticks ({y_ticks})")
    # y_ticks[0] => 6 stars, y_ticks[-1] => 0 stars (top-to-bottom)
    n_steps = len(y_ticks) - 1
    top_row, bot_row = y_ticks[0], y_ticks[-1]
    top_val, bot_val = float(n_steps), 0.0   # typically 6 -> 0

    def row_to_stars(r: float) -> float:
        # Linear: row=top_row -> top_val ; row=bot_row -> bot_val
        return top_val + (r - top_row) * (bot_val - top_val) / (bot_row - top_row)

    out: list[tuple[str, float]] = []
    for i, xc in enumerate(x_ticks):
        lo, hi = max(0, xc - 3), min(arr.shape[1], xc + 4)
        rows: list[int] = []
        for c in range(lo, hi):
            ys = np.where(line_mask[:, c])[0]
            if ys.size:
                rows.append(int(ys.min()))
        if rows:
            y = float(np.median(rows))
            stars = row_to_stars(y)
            # Keep 0.1★ resolution - Paul Scorer's formula produces real values,
            # not half-integers, though many days happen to sit on 0.5 steps.
            stars = round(stars, 1)
            if stars < 0:
                stars = 0.0
        else:
            stars = 0.0
        minutes = HOUR_START * 60 + SLOT_MIN * i
        hh, mm = divmod(minutes, 60)
        out.append((f"{hh:02d}:{mm:02d}", stars))
    return out


# -----------------------------------------------------------------------------
# Day summarisation

# --- Fly-score -----------------------------------------------------------
# Internal intermediate = "star-hours above the 1* threshold" (area under
# the curve above the flyable floor).  It rewards both peak height AND
# duration while ignoring the unflyable early/late tails.
#
# We then normalise to a 0-5 scale so the calendar title reads like a
# familiar star rating.  FLY_SCORE_ANCHOR is the "full 5-star" day.
#
# Threshold + anchor calibration:
# 2 stars is the practical floor for any cross-country flying - below that
# you might soar locally but you're not getting away.  So the fly score
# integrates star-hours above the 2* threshold, which directly answers
# "is this an XC-worthy day?".
#
# A realistic UK 5* day is sun-driven and follows the shape:
#   10:00-11:00  3-4*  ramp-up
#   11:00-12:00  4-5*  ramp
#   12:00-17:00  5*    plateau
#   17:00-19:00  3-4*  ramp-down
# That gives ~22 star-hours above 2*, not a flat 24h of 5* (which
# doesn't exist at these latitudes).  Anchoring 18 sh to 5.0 leaves a
# little headroom so a sustained 5* day is unambiguously 5*:
#
#   5 *   exceptional UK day - sustained 5* core (1-2 per year)
#   4 *   great XC day - 4* plateau plus healthy shoulders (~14 sh)
#   3 *   solid, committable XC (~10 sh)
#   2 *   marginal XC, mostly local (~6 sh)
#   1 *   brief XC-able window only (~3 sh)
#   0 *   not XC-able (peak < 2*)
#
# Tune this after a season of real-world feedback.
FLY_SCORE_THRESHOLD = 2.0   # stars - XC-able floor
FLY_SCORE_ANCHOR = 18.0     # star-hours above threshold that maps to 5.0


@dataclass
class DaySummary:
    date: dt.date
    model: str
    peak: float
    mean: float
    soarable_hours: float          # hours at >= 1.0 stars
    good_hours: float              # hours at >= 2.0 stars (XC-able)
    great_hours: float             # hours at >= 3.0 stars
    star_hours: float              # star-hours above FLY_SCORE_THRESHOLD (raw)
    fly_score: float               # normalised 0-5 star rating
    peak_start: str                # HH:MM where peak first reached
    peak_end: str                  # HH:MM where peak last held
    xc_start: str                  # first HH:MM at >= 2 stars (or "")
    xc_end: str                    # last HH:MM at >= 2 stars (or "")
    slots: list[tuple[str, float]]
    png: bytes = b""               # raw PNG bytes for embedding in ICS
    # Provenance.  fetched_utc is when the chart was RETRIEVED FROM RASP,
    # which is not the same as when this build ran - a carried-forward
    # chart keeps its original stamp so the calendar can state it.
    fetched_utc: dt.datetime | None = None
    source_model: str = ""         # model that produced the chart we hold
    origin: str = "fresh"          # "fresh" | "cached" | "carried"


@dataclass
class DayFailure:
    """
    One forecast day we have nothing publishable for.

    A day only lands here once the chart we hold has passed its expiry in
    FRESHNESS - i.e. the overnight boundary went by and no fetch has
    succeeded since.  Short of that, a failed fetch reuses the chart we
    already have and the entry states when it was retrieved.

    The original rule here was "never fall back to a previously-fetched
    value: a stale rating presented as current is worse than none".  The
    principle stands and this is still an implementation of it - the
    reader always CAN tell the difference, because the retrieval time is
    on the entry.  What changed is the recognition that blanking a chart
    fetched two hours ago destroys data that is still perfectly good, and
    that a blank week is worse for the reader than an honestly-labelled
    one.
    """
    date: dt.date
    model: str
    reason: str            # human-readable, shown in the calendar entry
    kind: str              # "stale" | "fetch" | "parse"


def summarise(date: dt.date, model: str, slots: list[tuple[str, float]]) -> DaySummary:
    values = np.array([v for _, v in slots])
    peak = float(values.max())
    mean = float(values.mean())
    soarable = float((values >= 1.0).sum()) * 0.5
    good = float((values >= 2.0).sum()) * 0.5
    great = float((values >= 3.0).sum()) * 0.5
    # Raw integrated star-hours above threshold (each slot is 0.5 h).
    star_hours = float(np.clip(values - FLY_SCORE_THRESHOLD, 0, None).sum()) * 0.5
    # Normalise to 0-5, capping at the anchor.
    fly_score = min(5.0, star_hours * 5.0 / FLY_SCORE_ANCHOR)
    peak_mask = values == peak
    peak_times = [t for t, _ in np.array(slots, dtype=object)[peak_mask]]
    # XC window: first..last slot at >= 2 stars.  Outer envelope - any sub-2*
    # interruption within that window is implicit in the sparkline.
    xc_idx = np.where(values >= 2.0)[0]
    if xc_idx.size:
        xc_start = slots[int(xc_idx[0])][0]
        xc_end   = slots[int(xc_idx[-1])][0]
    else:
        xc_start = ""
        xc_end = ""
    return DaySummary(
        date=date, model=model, peak=peak, mean=mean,
        soarable_hours=soarable, good_hours=good, great_hours=great,
        star_hours=round(star_hours, 1),
        fly_score=round(fly_score, 1),
        peak_start=peak_times[0] if peak_times else "",
        peak_end=peak_times[-1] if peak_times else "",
        xc_start=xc_start,
        xc_end=xc_end,
        slots=slots,
    )


# -----------------------------------------------------------------------------
# Writers

def write_csv(path: Path, summaries: list[DaySummary]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "model", "time", "stars"])
        for s in summaries:
            for t, v in s.slots:
                w.writerow([s.date.isoformat(), s.model, t, v])


def write_summary_csv(path: Path, summaries: list[DaySummary]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "date", "day", "model", "fly_score_0_5", "star_hours",
            "peak_stars", "mean_stars",
            "soarable_hours_>=1", "good_hours_>=2", "great_hours_>=3",
            "peak_start", "peak_end",
        ])
        for s in summaries:
            w.writerow([
                s.date.isoformat(), s.date.strftime("%a"), s.model,
                s.fly_score, s.star_hours, s.peak, round(s.mean, 2),
                s.soarable_hours, s.good_hours, s.great_hours,
                s.peak_start, s.peak_end,
            ])


def _ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(",", "\\,")
                .replace(";", "\\;").replace("\n", "\\n"))


def _spark(values: list[float]) -> str:
    """
    8-level Unicode block sparkline (▁▂▃▄▅▆▇█) of a stars curve.
    Sub-0.3* renders as a space so dead time is visibly empty.
    Glyphs are full-width across system fonts (proportional or not),
    so the sparkline aligns even in Outlook / Gmail rendering.
    """
    blocks = " ▁▂▃▄▅▆▇█"
    out = []
    for v in values:
        v = max(0.0, min(6.0, v))
        idx = int(round(v / 6.0 * 8))   # 0..8
        out.append(blocks[idx])
    return "".join(out)


def _xc_hours(s: "DaySummary") -> float:
    """Hours at >= FLY_SCORE_THRESHOLD stars (cached for body text)."""
    return s.good_hours   # threshold is 2.0; good_hours already counts >=2


def _build_description(s: "DaySummary", location: str,
                       generated_utc: dt.datetime) -> str:
    """
    Plain-text DESCRIPTION shown by every calendar client (incl. Google).
    Order: headline, XC window, peak, sparkline, generation timestamp.
    """
    head = f"{s.date.strftime('%a %d %b %Y')} ({s.model}) - {location}"
    body_lines = [head]
    if s.xc_start:
        body_lines.append(
            f"XC window (≥2★): {s.xc_start}-{s.xc_end} "
            f"({_xc_hours(s):.1f}h)"
        )
        peak_bits = [
            f"Peak {s.peak:.1f}★ at {s.peak_start}"
            + (f"-{s.peak_end}" if s.peak_end != s.peak_start else ""),
            f"mean {s.mean:.1f}★",
        ]
        if s.great_hours > 0:
            peak_bits.append(f"≥3★ for {s.great_hours:.1f}h")
        body_lines.append(", ".join(peak_bits))
    elif s.peak >= 1.0:
        body_lines.append(
            f"No XC window - peak only {s.peak:.1f}★ at {s.peak_start}"
        )
    else:
        body_lines.append(f"Not flyable - peak only {s.peak:.1f}★")
    spark_chars = _spark([v for _, v in s.slots])
    first_t = s.slots[0][0]
    last_t  = s.slots[-1][0]
    body_lines += [
        "",
        f"{first_t} {spark_chars} {last_t}",
        "(each block = 30 min, height ∝ stars)",
        "",
        *_provenance_lines(s, generated_utc),
    ]
    return "\n".join(body_lines)


def _provenance_lines(s: "DaySummary",
                      generated_utc: dt.datetime) -> list[str]:
    """
    What the reader needs to judge the rating for themselves: when the
    chart was actually retrieved, whether it has been refreshed since,
    and whether it came from a coarser model than this slot wants.
    """
    when = s.fetched_utc or generated_utc
    lines = [f"Chart retrieved {when.strftime('%Y-%m-%d %H:%M UTC')}"]
    if s.origin != "fresh":
        hours = (generated_utc - when).total_seconds() / 3600
        lines.append(
            f"Not refreshed since - {hours:.1f}h old as of "
            f"{generated_utc.strftime('%H:%M UTC')}"
        )
    if s.source_model and s.source_model != s.model:
        lines.append(
            f"From the {s.source_model} run - {s.model} not yet available"
        )
    return lines


def _expires_at(fetched: dt.datetime, hour: int) -> dt.datetime:
    """First `hour`:00 UTC strictly after `fetched`."""
    boundary = fetched.replace(hour=hour, minute=0, second=0, microsecond=0)
    if boundary <= fetched:
        boundary += dt.timedelta(days=1)
    return boundary


def _chart_filename(date: dt.date, model: str) -> str:
    return f"{date.isoformat()}_{model.replace('+', '_')}.png"


def _http_bytes(url: str, timeout: int = FETCH_TIMEOUT) -> bytes:
    req = Request(url, headers={"User-Agent": "rasp-stars-poller/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def load_published_state(base_url: str | None) -> dict[str, dict]:
    """
    Read the previous run's build_state.json straight off the published
    site, keyed by ISO date.

    The Pages artifact already contains both the state file and every
    chart PNG, so the live site IS the last-known-good store: no cache to
    configure, no extra storage, nothing to evict, and it is by definition
    exactly what subscribers are currently being served.  A cold start (no
    site yet) or an unreachable site just means no fallback this run.
    """
    if not base_url:
        return {}
    url = base_url.rstrip("/") + "/build_state.json"
    try:
        state = json.loads(_http_bytes(url).decode("utf-8"))
    except Exception as e:
        print(f"  no previous state from {url}: {e}", file=sys.stderr)
        return {}
    # Days written before provenance existed carry no per-day stamp; the
    # build-level timestamp is the best available answer for those.
    fallback_stamp = state.get("generated_utc", "")
    out: dict[str, dict] = {}
    for d in state.get("days", []):
        if d.get("status") not in ("ok", "cached", "carried"):
            continue
        stamp = d.get("fetched_utc") or fallback_stamp
        try:
            fetched = dt.datetime.strptime(
                stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        except (ValueError, TypeError):
            continue
        out[d["date"]] = {
            "source_model": d.get("source_model") or d.get("model", ""),
            "fetched_utc": fetched,
        }
    return out


def fetch_published_chart(base_url: str, date: dt.date,
                          source_model: str) -> bytes | None:
    """Pull one already-published chart PNG back off the live site."""
    url = (base_url.rstrip("/") + "/charts/"
           + _chart_filename(date, source_model))
    try:
        data = _http_bytes(url)
    except Exception as e:
        print(f"    held chart not retrievable ({url}): {e}", file=sys.stderr)
        return None
    if not data.startswith(b"\x89PNG"):
        print(f"    held chart is not a PNG ({url})", file=sys.stderr)
        return None
    return data


PLACEHOLDER_SUMMARY = "⟳ RASP data unavailable"


def _build_placeholder_description(f: "DayFailure", location: str,
                                   generated_utc: dt.datetime,
                                   retry_note: str) -> str:
    """
    Plain-text DESCRIPTION for a day whose chart could not be read.

    States plainly that there is no forecast for this day rather than
    implying a zero-star day, and tells the reader a retry is coming.

    Deliberately free of diagnostics.  Why the fetch failed - HTTP status,
    intercept-page title, parser message - is operational detail that
    belongs in the Actions log and build_state.json, not in a calendar
    entry someone reads on their phone.  f.reason is NOT rendered here.
    """
    return "\n".join([
        f"{f.date.strftime('%a %d %b %Y')} ({f.model}) - {location}",
        "",
        "No forecast available for this day.",
        "The RASP forecast could not be retrieved.",
        "",
        "Deliberately left blank rather than showing an out-of-date "
        "rating from an earlier run.",
        retry_note,
        "",
        f"Last attempt {generated_utc.strftime('%Y-%m-%d %H:%M UTC')}",
    ])


def _build_placeholder_html(f: "DayFailure", location: str,
                            generated_utc: dt.datetime,
                            retry_note: str) -> str:
    """Rich HTML twin of _build_placeholder_description (X-ALT-DESC)."""
    text = _build_placeholder_description(f, location, generated_utc,
                                          retry_note)
    head, *rest = text.split("\n")
    body = "".join(
        f'<p style="margin:0 0 6px 0;color:#555">{ln}</p>'
        for ln in rest if ln.strip()
    )
    return (
        '<html><body style="font-family:-apple-system,system-ui,sans-serif">'
        f'<p style="margin:0 0 4px 0;font-size:18px;color:#a33">'
        f'<b>{PLACEHOLDER_SUMMARY}</b></p>'
        f'<p style="margin:0 0 8px 0;color:#444">{head}</p>'
        f'{body}'
        '</body></html>'
    )


def _build_html(s: "DaySummary", location: str, b64: str,
                generated_utc: dt.datetime) -> str:
    """
    Rich HTML for X-ALT-DESC.  Renders in Apple Calendar reliably and in
    Outlook/MS365 most of the time.  Inline styles only - external CSS
    and class= attributes are stripped by most calendar clients.
    """
    date_h = s.date.strftime("%A %d %b %Y")
    if s.xc_start:
        xc_html = (
            f"<b>XC window (≥2★):</b> {s.xc_start}&ndash;{s.xc_end} "
            f"({_xc_hours(s):.1f}h)"
        )
        peak_bits = [
            f"Peak {s.peak:.1f}★ at {s.peak_start}"
            + (f"&ndash;{s.peak_end}" if s.peak_end != s.peak_start else ""),
            f"mean {s.mean:.1f}★",
        ]
        if s.great_hours > 0:
            peak_bits.append(f"&ge;3★ for {s.great_hours:.1f}h")
        peak_h = ", ".join(peak_bits)
    elif s.peak >= 1.0:
        xc_html = (
            f"<b>No XC window</b> &mdash; peak only {s.peak:.1f}★ "
            f"at {s.peak_start}"
        )
        peak_h = f"Mean {s.mean:.1f}★"
    else:
        xc_html = (
            f"<b>Not flyable</b> &mdash; peak only {s.peak:.1f}★"
        )
        peak_h = ""
    # Half-hour table: time | stars | bar.  Width = stars * 36px (5* = 180px).
    rows = []
    for t, v in s.slots:
        bar_px = int(round(v * 36))
        is_xc = v >= 2.0
        td_time = (
            f'<td style="text-align:right;color:#888;'
            f'font-variant-numeric:tabular-nums;'
            f'padding:1px 8px 1px 0">{t}</td>'
        )
        td_num = (
            f'<td style="text-align:right;'
            f'font-variant-numeric:tabular-nums;'
            f'padding:1px 6px 1px 0;'
            f'{"font-weight:600" if is_xc else ""}">'
            f'{v:.1f}</td>'
        )
        bar_color = "#cc4ecc" if is_xc else "#ee82ee"
        td_bar = (
            f'<td style="padding:1px 0">'
            f'<div style="width:{bar_px}px;height:11px;'
            f'background:{bar_color};border-radius:2px"></div>'
            f'</td>'
        )
        tr_style = ' style="background:#f7eaf7"' if is_xc else ""
        rows.append(f"<tr{tr_style}>{td_time}{td_num}{td_bar}</tr>")
    table = (
        '<table cellspacing="0" cellpadding="0" '
        'style="border-collapse:collapse;'
        'font-family:-apple-system,system-ui,sans-serif;'
        'font-size:13px;margin-top:8px">'
        + "".join(rows) + "</table>"
    )
    data_uri = f"data:image/png;base64,{b64}"
    return (
        '<html><body style="font-family:-apple-system,system-ui,sans-serif">'
        f'<p style="margin:0 0 4px 0;font-size:18px">'
        f'<b>{s.fly_score:.1f} ★</b> &mdash; {date_h}</p>'
        f'<p style="margin:0;color:#444">{location} ({s.model})</p>'
        f'<p style="margin:8px 0 4px 0">{xc_html}</p>'
        f'<p style="margin:0 0 8px 0;color:#555">{peak_h}</p>'
        f'<p style="margin:8px 0 0 0;color:#777;font-size:12px">'
        f'Half-hour breakdown (≥2★ highlighted):</p>'
        f'{table}'
        f'<p style="margin:12px 0 4px 0;color:#777;font-size:12px">'
        f'Original RASP chart:</p>'
        f'<p style="margin:0"><img src="{data_uri}" '
        f'alt="RASP stars chart" style="max-width:100%"></p>'
        f'<p style="margin:12px 0 0 0;color:#999;font-size:11px">'
        f'{"<br>".join(_provenance_lines(s, generated_utc))}</p>'
        '</body></html>'
    )


def _fold_ics_line(line: str) -> str:
    """
    Fold a long ICS content line at 75-octet boundaries per RFC 5545 s3.1.
    Continuation lines are prefixed with a single space.  Octet-based
    (not char-based) so multi-byte UTF-8 sequences like U+2605 are
    measured correctly; splits are kept on UTF-8 character boundaries.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts: list[str] = []
    # First segment: up to 75 octets, kept on a char boundary.
    # Subsequent: single space prefix + up to 74 additional octets.
    first = True
    i = 0
    while i < len(raw):
        budget = 75 if first else 74
        end = min(i + budget, len(raw))
        # Walk back if we're mid-multibyte sequence.
        while end > i and (raw[end - 1] & 0xC0) == 0x80:
            end -= 1
        # If we stopped on a leading byte of a multi-byte seq but before
        # its continuation bytes, walk back one more.
        if end < len(raw) and (raw[end] & 0xC0) == 0x80:
            # We're still inside a sequence - walk back to its start.
            while end > i and (raw[end] & 0xC0) == 0x80:
                end -= 1
        chunk = raw[i:end].decode("utf-8")
        parts.append(chunk if first else " " + chunk)
        first = False
        i = end
    return "\r\n".join(parts)


def write_ics(path: Path, summaries: list[DaySummary], location: str,
              failures: list[DayFailure] | None = None,
              retry_note: str = RETRY_NOTE_DEFAULT) -> None:
    generated_utc = dt.datetime.now(dt.timezone.utc)
    now = generated_utc.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//rasp-cambridge//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:RASP Stars - {location}",
        f"X-WR-CALDESC:Daily RASP star-rating forecast for {location}",
    ]
    for s in summaries:
        # Calendar title: just "<fly_score> ★" on the 0-5 scale.
        summary = f"{s.fly_score:.1f} \u2605"
        description = _build_description(s, location, generated_utc)
        dtstart = s.date.strftime("%Y%m%d")
        dtend = (s.date + dt.timedelta(days=1)).strftime("%Y%m%d")
        uid = f"rasp-{location.lower()}-{dtstart}@rasp-cambridge"
        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{dtstart}",
            f"DTEND;VALUE=DATE:{dtend}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(description)}",
            f"LOCATION:{_ics_escape(location)}",
        ]
        # Embed the RASP stars PNG as an inline attachment, plus a rich HTML
        # alternative with the half-hour table.  BASE64-encoded binary per
        # RFC 5545 s3.8.1.1.  Apple Calendar and MS Outlook honour both;
        # Google Calendar strips them and falls back to plain DESCRIPTION.
        if s.png:
            b64 = base64.b64encode(s.png).decode("ascii")
            fname = f"rasp_{location.lower()}_{dtstart}_{s.model.replace('+', '_')}.png"
            attach = (
                f"ATTACH;FMTTYPE=image/png;ENCODING=BASE64;VALUE=BINARY;"
                f"X-APPLE-FILENAME={fname};FILENAME={fname}:{b64}"
            )
            event_lines.append(attach)
            html = _build_html(s, location, b64, generated_utc)
            event_lines.append(
                f"X-ALT-DESC;FMTTYPE=text/html:{_ics_escape(html)}"
            )
        event_lines += [
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
        # Fold every event content line at 75 octets per RFC 5545 s3.1.
        lines += [_fold_ics_line(ln) for ln in event_lines]

    # Placeholder events for days we could not rate.  Same UID scheme as the
    # real events, so a later run that succeeds replaces the placeholder
    # rather than sitting alongside it.
    for f in (failures or []):
        description = _build_placeholder_description(
            f, location, generated_utc, retry_note)
        html = _build_placeholder_html(
            f, location, generated_utc, retry_note)
        dtstart = f.date.strftime("%Y%m%d")
        dtend = (f.date + dt.timedelta(days=1)).strftime("%Y%m%d")
        uid = f"rasp-{location.lower()}-{dtstart}@rasp-cambridge"
        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{dtstart}",
            f"DTEND;VALUE=DATE:{dtend}",
            f"SUMMARY:{_ics_escape(PLACEHOLDER_SUMMARY)}",
            f"DESCRIPTION:{_ics_escape(description)}",
            f"X-ALT-DESC;FMTTYPE=text/html:{_ics_escape(html)}",
            f"LOCATION:{_ics_escape(location)}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
        lines += [_fold_ics_line(ln) for ln in event_lines]

    lines.append("END:VCALENDAR")
    path.write_text("\r\n".join(lines) + "\r\n")


def write_state(path: Path, summaries: list[DaySummary],
                failures: list[DayFailure], today: dt.date) -> dict:
    """
    Record what this run published and where each day came from.

    This file has two readers.  The workflow uses the top-level fields to
    decide whether to build at all.  The NEXT run uses the per-day
    `fetched_utc` / `source_model` to find and age the charts it already
    holds - which is why this file is published alongside the charts
    rather than kept only in git.
    """
    now = dt.datetime.now(dt.timezone.utc)
    days = (
        [{"date": s.date.isoformat(),
          "model": s.model,
          "source_model": s.source_model or s.model,
          # "ok" = fetched this run; "cached" = held, inside its refresh
          # window; "carried" = held because the refresh failed.  The next
          # run reads all three back as usable.
          "status": "ok" if s.origin == "fresh" else s.origin,
          "fetched_utc": (s.fetched_utc or now)
                           .strftime("%Y-%m-%dT%H:%M:%SZ")}
         for s in summaries]
        + [{"date": f.date.isoformat(), "model": f.model,
            "status": f.kind, "reason": f.reason}
           for f in failures]
    )
    days.sort(key=lambda d: d["date"])
    fresh = sum(1 for s in summaries if s.origin == "fresh")
    state = {
        "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_date": today.isoformat(),
        "complete": not failures,
        "days_ok": len(summaries),        # publishable, however obtained
        "days_fresh": fresh,              # actually fetched this run
        "days_held": len(summaries) - fresh,
        "days_failed": len(failures),
        "days": days,
    }
    path.write_text(json.dumps(state, indent=2) + "\n")
    return state


# -----------------------------------------------------------------------------
# Main

def run(location: str, lat: float, lon: float,
        out_dir: Path, ics: Path | None,
        today: dt.date | None = None,
        state: Path | None = None,
        retry_note: str = RETRY_NOTE_DEFAULT,
        previous_base: str | None = None,
        ) -> tuple[list[DaySummary], list[DayFailure]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    today = today or dt.date.today()
    now = dt.datetime.now(dt.timezone.utc)
    previous = load_published_state(previous_base)
    summaries: list[DaySummary] = []
    failures: list[DayFailure] = []
    for i, model in enumerate(DAY_MODELS):
        date = today + dt.timedelta(days=i)
        refresh_after, expire_hour = FRESHNESS.get(i, FRESHNESS_DEFAULT)
        held = previous.get(date.isoformat())
        age_min = None
        held_ok = False
        if held:
            age_min = (now - held["fetched_utc"]).total_seconds() / 60
            held_ok = now < _expires_at(held["fetched_utc"], expire_hour)
        # Fetch when we hold nothing, when what we hold is due a refresh,
        # OR when it has expired.  That last clause matters: expiry always
        # forces one more attempt, otherwise a long refresh interval could
        # let a chart expire before we ever tried to replace it, and the
        # day would blank for hours waiting for its own refresh window.
        want_fetch = held is None or not held_ok or age_min >= refresh_after

        png: bytes | None = None
        origin, fetched, src_model = "fresh", now, model
        err: tuple[str, str] | None = None
        if want_fetch:
            try:
                png = fetch_png(model, lat, lon)
            except StaleChartError as e:
                print(f"  [{date} {model}] STALE: {e}", file=sys.stderr)
                err = ("stale", str(e))
            except NotAChartError as e:
                print(f"  [{date} {model}] NOT A CHART: {e}", file=sys.stderr)
                err = ("intercepted", str(e))
            except Exception as e:
                print(f"  [{date} {model}] fetch failed: {e}", file=sys.stderr)
                err = ("fetch", str(e))
        else:
            print(f"  [{date} {model}] holding - {age_min:.0f}m old, "
                  f"refresh due at {refresh_after}m", file=sys.stderr)

        # Fall back to what we already published, if it is still in date.
        if png is None and held is not None and held_ok:
            cached = fetch_published_chart(
                previous_base, date, held["source_model"])
            if cached is not None:
                png = cached
                origin = "carried" if want_fetch else "cached"
                fetched = held["fetched_utc"]
                src_model = held["source_model"]

        if png is None:
            if err is not None:
                kind, reason = err
            elif held is not None and not held_ok:
                kind = "expired"
                reason = (f"chart retrieved "
                          f"{held['fetched_utc'].strftime('%Y-%m-%d %H:%M UTC')} "
                          f"expired at the {expire_hour:02d}:00 UTC boundary")
            else:
                kind = "fetch"
                reason = "held chart could not be retrieved from the site"
            failures.append(DayFailure(date, model, reason, kind))
            continue

        (out_dir / _chart_filename(date, src_model)).write_bytes(png)
        try:
            slots = parse_stars_png(png)
        except Exception as e:
            print(f"  [{date} {model}] parse failed: {e}", file=sys.stderr)
            failures.append(DayFailure(date, model, str(e), "parse"))
            continue
        s = summarise(date, model, slots)
        s.png = png
        s.fetched_utc = fetched
        s.source_model = src_model
        s.origin = origin
        summaries.append(s)
        print(
            f"  {date} ({date.strftime('%a')}) {model:>7}  "
            f"fly {s.fly_score:>3.1f}/5 ({s.star_hours:>4.1f} sh)  "
            f"peak {s.peak:.1f}* ({s.peak_start}-{s.peak_end})  "
            f"mean {s.mean:.2f}*  "
            f">=1*: {s.soarable_hours:>4.1f}h  >=2*: {s.good_hours:>4.1f}h  "
            f">=3*: {s.great_hours:>4.1f}h"
            + ("" if s.origin == "fresh"
               else f"  [{s.origin} from "
                    f"{s.fetched_utc.strftime('%d %b %H:%M')}Z]")
        )
    for f in failures:
        print(f"  {f.date} ({f.date.strftime('%a')}) {f.model:>7}  "
              f"NO DATA ({f.kind}) - placeholder published")
    write_csv(out_dir / "halfhour.csv", summaries)
    write_summary_csv(out_dir / "summary.csv", summaries)
    if ics is not None:
        write_ics(ics, summaries, location, failures, retry_note)
    if state is not None:
        write_state(state, summaries, failures, today)
    return summaries, failures


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--location", default=DEFAULT_NAME)
    p.add_argument("--lat", type=float, default=DEFAULT_LAT)
    p.add_argument("--lon", type=float, default=DEFAULT_LON)
    p.add_argument("--out-dir", type=Path, default=Path("./out"))
    p.add_argument("--ics", type=Path, default=None,
                   help="Write an ICS calendar file to this path")
    p.add_argument("--state", type=Path, default=None,
                   help="Write a JSON build-state file to this path "
                        "(also read back by the next run, off the "
                        "published site, to age the charts it holds)")
    p.add_argument("--previous-base", default=None,
                   help="Base URL of the published site, e.g. "
                        "https://user.github.io/repo - used to recover "
                        "the previous build_state.json and charts when a "
                        "fetch fails.  Omit to disable carry-forward.")
    args = p.parse_args()
    print(f"RASP Stars for {args.location} ({args.lat},{args.lon})")
    summaries, failures = run(args.location, args.lat, args.lon,
                              args.out_dir, args.ics, state=args.state,
                              previous_base=args.previous_base)
    held = [s for s in summaries if s.origin != "fresh"]
    if held:
        print(f"\n{len(held)} of {len(DAY_MODELS)} days published from "
              f"charts already held (retrieval time shown on each entry).")
    if failures:
        print(f"\n{len(failures)} of {len(DAY_MODELS)} days unavailable - "
              f"placeholders published; the next run will retry.")
    # Exit 0 either way: the placeholders are a valid, publishable result and
    # the build must still deploy them.  Completeness is signalled through
    # the state file, not the exit code.
