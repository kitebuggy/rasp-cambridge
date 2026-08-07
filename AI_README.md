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

1. **Never publish stale data *unlabelled*.** The rule is that a reader
   must never mistake an old rating for a current one. Until 2026-08-06 that
   was enforced by refusing to carry anything forward at all; it now works
   by carrying charts forward **with their retrieval time stated on the
   entry**, and blanking only once the data is genuinely obsolete.

   *What the rule was always for* (Jason, 2026-08-06): stopping a new day's
   fetch from silently reverting to a day-old view of the forecast. Two
   distinct ways that can happen, and both are still closed:
   - RASP serves yesterday's file for a model it has not regenerated →
     rejected by the `Last-Modified` guard in `fetch_png`.
   - A chart we fetched ourselves survives past the overnight model run →
     dropped by `expire_hour` (03:00 UTC) in `FRESHNESS`.

   What is *not* a violation is republishing a chart fetched a few hours
   ago, within the same forecast day, with its age on the entry.

   *Why it changed:* every run pruned, re-fetched all seven charts and
   redeployed, so one failed fetch **destroyed** good published data - a
   single timeout blanked the whole week for every subscriber. "We could not
   fetch" and "the forecast is unknown" are different things and only the
   second deserves a blank.

   The policy is the `FRESHNESS` table at the top of `rasp_stars.py`:
   `refresh_after` (when to try RASP again; today and tomorrow = always) and
   `expire_hour` (03:00 UTC - when a held chart stops being publishable
   however recently fetched). Expiry is a clock time, not a duration,
   because what invalidates a chart is the overnight model run, not elapsed
   hours. **Expiry also independently forces a fetch** - without that, a
   24 h refresh interval could let a chart expire before its own refresh
   window opened. Do not remove that clause.

   Still true: past `expire_hour` a day becomes a "⟳ RASP data unavailable"
   placeholder, replaced by retry (same UID). Still true: the prune step is
   deliberate - the build re-downloads what it keeps from the published
   site, so pruning does not lose anything. What is now WRONG to "restore":
   deleting the carry-forward path, or dropping the provenance lines from
   entries. If you find yourself removing the retrieval timestamps, you are
   reintroducing the exact confusion this rule exists to prevent.

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
   `public/build_state.json` (read by the decide step), the two CSVs, and
   `index.html`.

   The artifact has a **second job**: because it publishes both the charts
   and `build_state.json`, the live site is the build's last-known-good
   store (`--previous-base`). Breaking the deploy therefore also breaks
   carry-forward. Do not "tidy" the charts out of the artifact.

4. **`MAX_AGE_MIN` is a debounce, not the freshness policy.** One hourly
   cron, no anchor/recheck split, and a decide step that rebuilds unless the
   previous build was under `MAX_AGE_MIN` (45 min) ago. Freshness is decided
   per forecast day by `FRESHNESS` in `rasp_stars.py` (rule 1), *after* this
   step has said yes.

   It used to be the whole policy, and that failed silently: a single global
   age threshold is a FLOOR, not an interval - the real refresh period is
   the threshold plus the wait for the next slot GitHub actually dispatches.
   At 180 min it sat just above the measured 136-180 min gap between
   arrivals and skipped alternate surviving runs, halving the build rate
   with nothing failing. **Do not grow it back into a freshness knob** - one
   number cannot express "today matters more than next Tuesday".

   Also: do not reintroduce schedule classification (parsing
   `github.event.schedule`), and do not treat GitHub cron as reliable - it
   is measured lossy here (a third to two-thirds of slots dropped, delays to
   +75 min). See `docs/operations/scheduling.md` before touching the
   schedule or the minute.

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
   (add `--ics` / `--state` to exercise the writers, and `--previous-base
   https://kitebuggy.github.io/rasp-cambridge` to exercise carry-forward).
   Exit code is 0 even with failed days - completeness is signalled via the
   state file, not the exit code.
6. Changing freshness behaviour → the degraded paths are what matter, and
   they are cheap to test by stubbing `fetch_png` /
   `fetch_published_chart` / `load_published_state` and calling `run()`.
   Cover at least: all-fetch, total failure with charts in date, total
   failure with charts past 03:00Z, nothing held, mixed refresh windows,
   and coarser-model fallback. Note days +2..+6 usually make no request at
   all, so a "total failure" run hits RASP twice, not seven times.

## Common mistakes

- Removing the carry-forward path or the "Chart retrieved ..." provenance
  lines because an older comment says never to carry data forward. Rule 1
  changed on 2026-08-06 - read it before "restoring" the old behaviour.
- Conflating the two kinds of old data: a `StaleChartError` chart (RASP
  serving *yesterday's file* for a model it has not regenerated) is still
  rejected outright. Carry-forward reuses *our own* previously-fetched
  chart, dated on the entry. Never weaken the `Last-Modified` guard.
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
