# AI_README - rasp_cambridge

Entry point for AI agents (and anyone else) working in this repo. Read this
before changing anything. The `docs/` tree holds the detail; this file holds
the rules that must not be broken.

## Project overview

- **Owner:** Jason Holloway. Personal project, not a QL Security property.
- **What it is:** a subscribable ICS calendar of the next 7 days of RASP UK
  "stars" gliding forecasts for Cambridge, rebuilt through the day by GitHub
  Actions and served from GitHub Pages.
- **Stack:** one Python script (`rasp_stars.py`, stdlib + numpy + Pillow),
  one GitHub Actions workflow (`.github/workflows/build-rasp.yml`), GitHub
  Pages. No web framework, no database, no tests (a known, accepted gap).
- **GitHub repo:** `kitebuggy/rasp-cambridge` (local folder is
  `rasp_cambridge`). Default branch `main`. Licence GPLv3.
- **Data source:** `https://app.stratus.org.uk/blip/graph/blip_stars.php` -
  a PNG chart, parsed pixel by pixel. There is no API; the PNG is the only
  exposed source. RASP is a volunteer-run service (Paul Scorer,
  stratus.org.uk) - be a polite client.

## Critical rules

1. **Never publish stale forecast data.** A previous run's star rating is
   never carried forward - a day that cannot be fetched or parsed becomes a
   "⟳ RASP data unavailable" placeholder event, replaced by retry (same UID).
   Do not add caching, carry-forward, or "last known good" fallbacks. The
   workflow's prune step (deleting charts and the ICS before each build) is
   deliberate, not an oversight.

2. **Diagnostics never go in the ICS.** Why a fetch failed (HTTP status,
   intercept-page title, parser message) belongs in stderr (the Actions log)
   and in `build_state.json`. `_build_placeholder_description` must NOT
   render `f.reason`. Calendar entries say only that the forecast could not
   be retrieved and a retry is coming.

3. **Never commit the ICS or the chart PNGs.** GitHub Pages serves the
   Actions *artifact*, tarred from the runner's working tree - git tracking
   is irrelevant to subscribers, and tracking these files cost ~500 KB of
   permanent history per rebuild before it was stopped. `.gitignore` excludes
   them; the workflow's `git add public/` relies on that. What IS committed:
   `public/build_state.json` (the only cross-run persistence - the decide
   step reads it), the two CSVs, and `index.html`.

4. **`MAX_AGE_MIN` is the entire scheduling policy.** One hourly cron, no
   anchor/recheck split, and a decide step that rebuilds when data is
   missing, from the wrong UTC day, or older than `MAX_AGE_MIN`. The value
   is a FLOOR, not an interval - keep it below the measured minimum gap
   between slots GitHub actually dispatches, or it silently skips alternate
   runs. Do not reintroduce schedule classification (parsing
   `github.event.schedule`) and do not treat GitHub cron as reliable - it
   is measured lossy here (roughly a third to two-thirds of slots dropped,
   delays to +75 min). See `docs/operations/scheduling.md` before touching
   the schedule or the minute.

5. **Git discipline.** Jason runs all modifying git commands (add, commit,
   push, checkout, fetch...) himself - never run them from an AI session.
   Read-only git from a sandboxed/mounted session is allowed ONLY prefixed
   with `GIT_OPTIONAL_LOCKS=0` (plain `git status` writes `.git/index.lock`,
   which cannot be cleaned up through the mount and blocks Jason's next
   commit). After any git invocation, check `ls .git/*.lock` and report a
   stale lock immediately.

6. **No scratch files in the repo.** Remote sessions cannot delete files
   under the mounted repo. Backups and temp files go outside the repo
   (`~/tmp/`); `_to_delete/` inside the repo is a last resort only.

7. **CI hardening stays as it is.** Actions are pinned to full commit SHAs
   (version in trailing comment); `.github/dependabot.yml` keeps the pins
   current - do not remove it, and do not add a pip ecosystem (dependencies
   float by design). `upload-pages-artifact` must stay ≥ v5 (earlier majors
   pin a node20 upload-artifact). Never set
   `ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION`. Permissions are scoped per
   job - keep workflow-level `contents: read`.

8. **`requirements.txt` floats deliberately** (`numpy>=1.26`,
   `Pillow>=10.0`). Accepted risk, documented in README Caveats: the parser
   is pixel-archaeology with no tests, so a Pillow change to PNG decoding
   could shift ratings silently. A reference-PNG regression test is the
   largest open gap - offered and declined for now.

9. **Keep the maps current.** After adding, removing or restructuring files,
   update `docs/reference/filesystem-map.md` and (for code or workflow
   changes) `docs/reference/dependency-map.md`. Both are hand-maintained -
   there is no generator script in this repo.

## Documentation locations

| Doc | What it covers |
|-----|----------------|
| `README.md` | Human-facing: what it is, subscribing, forking, fly score, caveats |
| `docs/index.md` | Documentation index |
| `docs/architecture/pipeline.md` | Fetch → parse → score → ICS pipeline, chart geometry, guards |
| `docs/architecture/publishing.md` | Pages artifact model, commit policy, history-bloat rationale |
| `docs/operations/scheduling.md` | Cron + max-age design, GitHub cron measurements, diagnosis |
| `docs/operations/troubleshooting.md` | Intercepts/WAF, stale charts, parse failures, quiet mornings |
| `docs/reference/filesystem-map.md` | Annotated directory tree |
| `docs/reference/dependency-map.md` | External deps, internal call graph, artefact flow, couplings |

## Key file locations

- `rasp_stars.py` - the whole program. Location constants
  (`DEFAULT_LAT/LON/NAME`), chart constants (`LINE_RGB`, `N_SLOTS_*`), and
  fly-score tuning (`FLY_SCORE_THRESHOLD`, `FLY_SCORE_ANCHOR`) are near the
  top.
- `.github/workflows/build-rasp.yml` - schedule, decide step
  (`MAX_AGE_MIN`), build and deploy jobs. Heavily commented; the comments
  record decisions and are part of the documentation - keep them true.
- `public/` - the deployed site. Partly committed, partly artifact-only; see
  Critical rule 3 and `docs/architecture/publishing.md`.

## Before making changes

1. Read the relevant `docs/` page (table above) - most past incidents and
   decisions are recorded there or in the workflow comments.
2. Changing fetch/ICS/placeholder logic → re-read Critical rules 1 and 2.
3. Changing the workflow → re-read Critical rules 3, 4 and 7. Note the
   workflow triggers on push to `main` for changes to itself, so a pushed
   change is exercised immediately.
4. Changing the schedule → `docs/operations/scheduling.md` first, and treat
   any new minute choice as a hypothesis to measure, not a fix.
5. Verify Python changes run locally: `python3 rasp_stars.py --out-dir ./out`
   (add `--ics` / `--state` to exercise the writers). Exit code is 0 even
   with failed days - completeness is signalled via the state file, not the
   exit code.

## Common mistakes

- "Helpfully" falling back to yesterday's rating when RASP is down
  (violates rule 1 - the failure mode this repo is designed around).
- Putting failure detail into calendar entries (violates rule 2).
- Re-adding `public/cambridge_rasp.ics` or `public/charts/*.png` to git -
  the workflow's `git add public/` silently re-adds them if `.gitignore` is
  weakened. This happened twice before the ignore rules were added.
- Editing the schedule and assuming runs fire on time - check the
  measurements in `docs/operations/scheduling.md` first.
- Matching Actions runs to cron slots by eye - delays reach +75 min, so
  slots overtake each other. Read the decide-step log lines instead.
- Running bare `git status` from a sandbox session (violates rule 5).
- Quoting a precise rebuild timetable in user-facing text (README,
  index.html, placeholder events). GitHub does not honour the timetable;
  say "roughly hourly / every few hours" instead - see `RETRY_NOTE_DEFAULT`.

## Testing

There is no test suite. Verification is:

- Local run against live RASP: `python3 rasp_stars.py --out-dir ./out --ics
  ./out/test.ics --state ./out/state.json`, then eyeball the printed
  per-day lines, the CSVs, and the ICS in a calendar client.
- Workflow changes: `bash -n` any edited `run:` script, then push and watch
  the push-triggered run (the workflow self-triggers on its own path).
- The audit-trail PNGs under `public/charts/` make parser drift visible -
  compare a suspect day's PNG against its CSV rows.
