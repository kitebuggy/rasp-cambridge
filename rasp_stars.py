#!/usr/bin/env python3
"""
Fetch RASP UK 'Stars' forecast chart for a location (default: Cambridge)
and extract numeric values by parsing the PNG pixels.

Output:
  - CSV of half-hourly star ratings for each of the 7 forecast days
  - Optional ICS calendar file with one all-day event per day containing
    the peak + mean star rating in the summary.

Usage:
  python3 rasp_stars.py --out-dir ./out --ics cambridge_rasp.ics
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import io
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

# RASP charts always start at 0700.  Today's chart (UK4) runs to 2100;
# forecast days (UK4+N, UK12+N) run to 1900.  So N_SLOTS is 25 or 29.
HOUR_START = 7
N_SLOTS_FORECAST = 25   # 0700..1900 every 30 min
N_SLOTS_TODAY    = 29   # 0700..2100 every 30 min

# The stars curve is drawn as solid "violet" (238,130,238) PNG pixels.
LINE_RGB = (238, 130, 238)


# -----------------------------------------------------------------------------
# Helpers

def fetch_png(model: str, lat: float, lon: float) -> bytes:
    params = {
        "model": model,
        "lat": f"{lat:.5f}",
        "lon": f"{lon:.5f}",
    }
    url = f"{BASE}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "rasp-stars-poller/1.0"})
    with urlopen(req, timeout=30) as resp:
        data = resp.read()
    if not data.startswith(b"\x89PNG"):
        raise RuntimeError(f"Expected PNG, got {data[:16]!r} from {url}")
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
    if len(x_ticks) not in (N_SLOTS_FORECAST, N_SLOTS_TODAY):
        raise RuntimeError(
            f"Expected {N_SLOTS_FORECAST} or {N_SLOTS_TODAY} x-ticks, got "
            f"{len(x_ticks)}"
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
        minutes = HOUR_START * 60 + 30 * i
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
# Anchor calibration (Jason, April 2026):
# A realistic UK 5* day is sun-driven and follows the shape:
#   10:00-11:00  3-4*  ramp-up
#   11:00-12:00  4-5*  ramp
#   12:00-17:00  5*    plateau
#   17:00-19:00  3-4*  ramp-down
# That's ~30 star-hours above the 1* threshold - not a flat 24h of 5*,
# which doesn't exist at these latitudes.  We anchor 30 sh to 5.0 so:
#
#   5 *   genuinely exceptional UK day (1-2 per year, sustained 5* core)
#   4 *   great UK XC day - 4* plateau with healthy shoulders (~24 sh)
#   3 *   solid, committable XC day (~18 sh)
#   2 *   local soaring + maybe a small task (~12 sh)
#   1 *   scratchy local only (~6 sh)
#   0 *   not flyable
#
# Tune this after a season of real-world feedback.
FLY_SCORE_THRESHOLD = 1.0   # stars
FLY_SCORE_ANCHOR = 30.0     # star-hours above threshold that maps to 5.0


@dataclass
class DaySummary:
    date: dt.date
    model: str
    peak: float
    mean: float
    soarable_hours: float          # hours at >= 1.0 stars
    good_hours: float              # hours at >= 2.0 stars
    great_hours: float             # hours at >= 3.0 stars
    star_hours: float              # star-hours above FLY_SCORE_THRESHOLD (raw)
    fly_score: float               # normalised 0-5 star rating
    peak_start: str                # HH:MM where peak first reached
    peak_end: str                  # HH:MM where peak last held
    slots: list[tuple[str, float]]
    png: bytes = b""               # raw PNG bytes for embedding in ICS


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
    return DaySummary(
        date=date, model=model, peak=peak, mean=mean,
        soarable_hours=soarable, good_hours=good, great_hours=great,
        star_hours=round(star_hours, 1),
        fly_score=round(fly_score, 1),
        peak_start=peak_times[0] if peak_times else "",
        peak_end=peak_times[-1] if peak_times else "",
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


def write_ics(path: Path, summaries: list[DaySummary], location: str) -> None:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//QL Security//RASP Cambridge//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:RASP Stars - {location}",
        "X-WR-CALDESC:Daily RASP star-rating forecast for Cambridge",
    ]
    for s in summaries:
        # Calendar title: just "<fly_score> ★" on the 0-5 scale.
        summary = f"{s.fly_score:.1f} \u2605"
        description = (
            f"RASP {s.model} for {location}\\n"
            f"Fly score {s.fly_score:.1f}/5 "
            f"(from {s.star_hours:.1f} star-hours above 1*)\\n"
            f"Peak {s.peak:.1f}* (held {s.peak_start}-{s.peak_end})\\n"
            f"Mean {s.mean:.1f}*\\n"
            f">=1* for {s.soarable_hours:.1f}h, >=2* for {s.good_hours:.1f}h, "
            f">=3* for {s.great_hours:.1f}h"
        )
        dtstart = s.date.strftime("%Y%m%d")
        dtend = (s.date + dt.timedelta(days=1)).strftime("%Y%m%d")
        uid = f"rasp-{location.lower()}-{dtstart}@qlsecurity.co.uk"
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
        # Embed the RASP stars PNG as an inline attachment.  BASE64-encoded
        # binary per RFC 5545 s3.8.1.1; filename hints for clients that
        # render/export the attachment.  Apple Calendar and MS Outlook
        # both honour this; Google Calendar does not.
        if s.png:
            b64 = base64.b64encode(s.png).decode("ascii")
            fname = f"rasp_{location.lower()}_{dtstart}_{s.model.replace('+', '_')}.png"
            attach = (
                f"ATTACH;FMTTYPE=image/png;ENCODING=BASE64;VALUE=BINARY;"
                f"X-APPLE-FILENAME={fname};FILENAME={fname}:{b64}"
            )
            event_lines.append(attach)
        # Richer HTML description with an inline data-URI image, for clients
        # (notably Outlook/MS365) that honour X-ALT-DESC.
        if s.png:
            data_uri = f"data:image/png;base64,{b64}"
            date_h = s.date.strftime("%A %d %b %Y")
            html = (
                "<html><body>"
                f"<p><b>{s.fly_score:.1f} \u2605  -  {date_h}</b></p>"
                f"<p>RASP {s.model} for {location}<br>"
                f"Fly score {s.fly_score:.1f}/5 "
                f"(from {s.star_hours:.1f} star-hours above 1*)<br>"
                f"Peak {s.peak:.1f}* ({s.peak_start}-{s.peak_end}),"
                f" mean {s.mean:.1f}*<br>"
                f"&ge;1* for {s.soarable_hours:.1f}h, "
                f"&ge;2* for {s.good_hours:.1f}h, "
                f"&ge;3* for {s.great_hours:.1f}h</p>"
                f'<p><img src="{data_uri}" alt="RASP stars chart"></p>'
                "</body></html>"
            )
            event_lines.append(
                f"X-ALT-DESC;FMTTYPE=text/html:{_ics_escape(html)}"
            )
        event_lines += [
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
        # Fold every event content line at 75 octets per RFC 5545 s3.1.
        lines += [_fold_ics_line(ln) for ln in event_lines]
    lines.append("END:VCALENDAR")
    path.write_text("\r\n".join(lines) + "\r\n")


# -----------------------------------------------------------------------------
# Main

def run(location: str, lat: float, lon: float,
        out_dir: Path, ics: Path | None,
        today: dt.date | None = None) -> list[DaySummary]:
    out_dir.mkdir(parents=True, exist_ok=True)
    today = today or dt.date.today()
    summaries: list[DaySummary] = []
    for i, model in enumerate(DAY_MODELS):
        date = today + dt.timedelta(days=i)
        try:
            png = fetch_png(model, lat, lon)
        except Exception as e:
            print(f"  [{date} {model}] fetch failed: {e}", file=sys.stderr)
            continue
        (out_dir / f"{date.isoformat()}_{model.replace('+', '_')}.png").write_bytes(png)
        try:
            slots = parse_stars_png(png)
        except Exception as e:
            print(f"  [{date} {model}] parse failed: {e}", file=sys.stderr)
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
    write_csv(out_dir / "halfhour.csv", summaries)
    write_summary_csv(out_dir / "summary.csv", summaries)
    if ics is not None:
        write_ics(ics, summaries, location)
    return summaries


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--location", default=DEFAULT_NAME)
    p.add_argument("--lat", type=float, default=DEFAULT_LAT)
    p.add_argument("--lon", type=float, default=DEFAULT_LON)
    p.add_argument("--out-dir", type=Path, default=Path("./out"))
    p.add_argument("--ics", type=Path, default=None,
                   help="Write an ICS calendar file to this path")
    args = p.parse_args()
    print(f"RASP Stars for {args.location} ({args.lat},{args.lon})")
    run(args.location, args.lat, args.lon, args.out_dir, args.ics)
