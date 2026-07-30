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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
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
    with urlopen(req, timeout=30) as resp:
        data = resp.read()
        lm_header = resp.headers.get("Last-Modified", "")
        status = resp.status
        headers = resp.headers
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
    n_slots = len(x_ticks)
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


@dataclass
class DayFailure:
    """
    One forecast day we could not produce a rating for.

    We deliberately do NOT fall back to a previously-fetched value: a stale
    star rating presented as current is worse than no rating at all, because
    the reader has no way to tell the difference.  Instead the day is
    published as a placeholder event that says so, and the next scheduled
    run replaces it once RASP is serving the chart again.
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
        f"Forecast retrieved {generated_utc.strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    return "\n".join(body_lines)


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
        f'Forecast retrieved '
        f'{generated_utc.strftime("%Y-%m-%d %H:%M UTC")}</p>'
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
        "X-WR-CALDESC:Daily RASP star-rating forecast for Cambridge",
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
    Record what this run managed to fetch, so the next scheduled run can
    decide whether it needs to do anything at all.

    The hourly workflow reads this: if the last run covered today's date
    and every model came back fresh, there is nothing to recheck and the
    run exits in seconds.  Any gap means RASP was mid-refresh or down, and
    the next hourly run tries again.
    """
    days = (
        [{"date": s.date.isoformat(), "model": s.model, "status": "ok"}
         for s in summaries]
        + [{"date": f.date.isoformat(), "model": f.model,
            "status": f.kind, "reason": f.reason}
           for f in failures]
    )
    days.sort(key=lambda d: d["date"])
    state = {
        "generated_utc": dt.datetime.now(dt.timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_date": today.isoformat(),
        "complete": not failures,
        "days_ok": len(summaries),
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
        ) -> tuple[list[DaySummary], list[DayFailure]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    today = today or dt.date.today()
    summaries: list[DaySummary] = []
    failures: list[DayFailure] = []
    for i, model in enumerate(DAY_MODELS):
        date = today + dt.timedelta(days=i)
        try:
            png = fetch_png(model, lat, lon)
        except StaleChartError as e:
            print(f"  [{date} {model}] STALE: {e}", file=sys.stderr)
            failures.append(DayFailure(date, model, str(e), "stale"))
            continue
        except NotAChartError as e:
            print(f"  [{date} {model}] NOT A CHART: {e}", file=sys.stderr)
            failures.append(DayFailure(date, model, str(e), "intercepted"))
            continue
        except Exception as e:
            print(f"  [{date} {model}] fetch failed: {e}", file=sys.stderr)
            failures.append(DayFailure(date, model, str(e), "fetch"))
            continue
        (out_dir / f"{date.isoformat()}_{model.replace('+', '_')}.png").write_bytes(png)
        try:
            slots = parse_stars_png(png)
        except Exception as e:
            print(f"  [{date} {model}] parse failed: {e}", file=sys.stderr)
            failures.append(DayFailure(date, model, str(e), "parse"))
            continue
        s = summarise(date, model, slots)
        s.png = png
        summaries.append(s)
        print(
            f"  {date} ({date.strftime('%a')}) {model:>7}  "
            f"fly {s.fly_score:>3.1f}/5 ({s.star_hours:>4.1f} sh)  "
            f"peak {s.peak:.1f}* ({s.peak_start}-{s.peak_end})  "
            f"mean {s.mean:.2f}*  "
            f">=1*: {s.soarable_hours:>4.1f}h  >=2*: {s.good_hours:>4.1f}h  "
            f">=3*: {s.great_hours:>4.1f}h"
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
                        "(used by the workflow to decide whether an hourly "
                        "recheck is needed)")
    args = p.parse_args()
    print(f"RASP Stars for {args.location} ({args.lat},{args.lon})")
    summaries, failures = run(args.location, args.lat, args.lon,
                              args.out_dir, args.ics, state=args.state)
    if failures:
        print(f"\n{len(failures)} of {len(DAY_MODELS)} days unavailable - "
              f"placeholders published; an hourly recheck will retry.")
    # Exit 0 either way: the placeholders are a valid, publishable result and
    # the build must still deploy them.  Completeness is signalled through
    # the state file, not the exit code.
