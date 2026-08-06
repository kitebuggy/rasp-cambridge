# Scheduling: one cron + a max-age rule

## Current design (max-age since 2026-08-03; widened 2026-08-06)

```yaml
schedule:
  - cron: "45 * * * *"    # hourly, round the clock, all UTC
```

Every slot runs the same **decide step**, which rebuilds when ANY of:

- not a scheduled run (push or `workflow_dispatch`) - rebuild unconditionally
- no `public/build_state.json` yet
- state is for a different UTC day
- state incomplete (`days_failed` > 0)
- `generated_utc` older than `MAX_AGE_MIN` (currently 120 minutes)

Otherwise it exits in a couple of seconds. On a normal day that settles
into roughly five real builds (~35 chart requests against RASP), with the
rest as cheap no-ops - an overnight no-op costs about ten seconds. Every
failure mode of the decide script (truncated JSON, missing fields,
unreadable file) falls to `build=true` - the safe direction. A corrupt
state file prints Python tracebacks before deciding correctly; that is
inherited and deliberate, since the traceback only appears when you would
want it.

**`MAX_AGE_MIN` is the entire scheduling policy in one number - and it is a
FLOOR, not an interval.** The real refresh period is the floor plus the
wait for the next slot GitHub actually dispatches, measured at 136-180
minutes between arrivals over 2026-08-04..06. **Keep the floor below that
minimum gap:** at 180 it sat just above it and silently skipped alternate
surviving runs, turning a nominal 3-hour policy into a ~5-hour one (on
2026-08-06 the 10:47 run missed the cut by about a minute). Raise it to cut
load on RASP (a volunteer-run service - be polite; 240 gives ~3 builds a
day), lower it to track the volatile UK4 "today" curve. It only governs the
happy path: `complete=false` short-circuits the age check, so incomplete
days retry on every surviving slot regardless.

The schedule was `13 5-17 * * *` from 2026-08-03 (commit "Cron fixes") to
2026-08-06. It was widened to 24 hours because the 05:xx and 06:xx slots
were dropped every single day for six days, so the early-bird build never
happened - extra pre-dawn slots are the only cheap way to get a chance at
one.

### Why this replaced the anchor/recheck split

The previous design had three unconditional "anchor" crons plus a
conditional hourly "recheck" line, classified by parsing
`github.event.schedule`. Three problems, all fixed by max-age:

1. A dropped anchor was uncompensated - the next recheck saw complete state
   and skipped, so the UK4 "today" curve stayed stale until the next anchor.
2. Nothing fetched fresh data after midday, so RASP's 12Z run was never
   collected until the next morning. Under max-age the evening refresh
   falls out for free.
3. Classification relied on an unwritten invariant about cron formatting -
   editing the schedule could silently invert behaviour. With max-age there
   is nothing to classify, so the schedule is safe to edit.

Replaying Aug 2's real dispatch pattern through the new rule: 4 rebuilds
(08:32, 11:42, 14:50, 18:15 BST) versus 3 under the old design with the
last at 12:37 BST.

## GitHub cron is lossy - the measurements

Treat GitHub's scheduler as best-effort: runs are dispatched late under
load and **dropped outright** when load is high enough.

- **2026-07-29, crons at `:00`:** delays of +1, +16, +26, +49, +51 min;
  3 of 8 slots between 10:00 and 17:00 UTC never fired (~40% loss).
- **2026-07-31..08-02, crons at `:23`:** no better - 8/10/9 runs against a
  13-slot timetable (~30% loss), one dispatch **+74 min** late, and the
  05:23 and 06:23 slots had a **100% miss rate** across all three days.
- **2026-08-03: moved to `:13`.** Measured 2026-08-04..06: about **5 of 13
  slots arrived per day**, spaced 136-180 min apart - not hourly, and the
  pre-dawn blackout persisted.
- **2026-08-06: moved to `:45`** and widened to 24 h - on the theory that
  the contention window sits around the top of the hour and the far side of
  it is the cheapest place to be. Recorded so nobody re-litigates it: `:45`
  IS one of the quarter-hours the earlier reasoning avoided, and none of
  the previous minutes measurably beat the others. A cheap experiment, not
  a fix.

Two findings that constrain any future attempt:

1. **`createdAt` == `startedAt` to the second on every run** - there is no
   runner-queue latency; all lateness is GitHub *dispatching* the event.
   Tuning concurrency, runner labels or job setup cannot help.
2. **The delay pattern repeats daily to within ±3 minutes** - it tracks
   global cron load, not jitter, so another unpopular minute only shaves
   the edge. Redundancy (13 slots) + max-age is what buys reliability.

**Untested hypothesis:** delivery fell from 7-9 runs/day (four `- cron:`
entries) to ~5/day (one entry), so GitHub's dispatch budget may be per cron
*entry*, not per slot. Cheap test: add a second entry at a different
minute - harmless now that nothing depends on which cron fired.

If a firing ever must be guaranteed, drive `workflow_dispatch` from an
external scheduler via the API (n8n suits) rather than adding crons.
Offered more than once; still open.

## Diagnosing "the runs are at the wrong time" (or "stopped")

- **Check the remote run list FIRST.** The local clone proves nothing: the
  bot commits straight to `origin/main`, so a working copy is only as fresh
  as its last fetch, and reasoning from local git state has produced a
  confident, wrong "the workflow stopped" before (2026-08-06). The command
  that settles it (run by Jason locally):

  ```
  env -u GH_HOST gh run list -R kitebuggy/rasp-cambridge -w build-rasp.yml -L 20 \
    --json databaseId,event,createdAt,status,conclusion
  ```

  `gh workflow list --all` additionally shows `active` vs
  `disabled_manually` vs `disabled_inactivity` in one word.
- Actions cron is **UTC-only, no DST awareness**, and the Actions UI shows
  timestamps in the *viewer's* local zone (BST in summer) - a BST-looking
  run list proves nothing.
- The discriminator is the **minute**, not the hour: a timezone fault can
  only shift the hour, so runs would still start at the cron's minute.
  Scattered minutes = dispatch delay.
- Because both the crons and RASP's own regen (~06:50 UTC) are UTC, DST
  needs no compensation - local times just drift an hour at the clock
  change.
- **Do not match runs to slots by eye.** With +75 min delays, slots
  overtake each other; a run that looks early is the previous slot arriving
  very late. That misreading cost a diagnostic round on 2026-08-03. Read
  the decide step's log/summary line (it prints the age and reason) instead.

## Silent killers

- **60 days of repo inactivity** auto-disables scheduled workflows (banner
  in the Actions tab). Not a live risk while rebuilds keep committing
  `build_state.json` - one reason it stays tracked.
- Schedules are only read from the **default branch** - a cron change on a
  side branch does nothing until it lands on `main`.

## Tooling notes (for AI sessions)

- The cloud sandbox has no GitHub API access to this repo and `device_bash`
  has no network - run-history queries must be run by Jason locally with
  `gh`. His shell sets `GH_HOST` to a non-github.com host, so use
  `env -u GH_HOST gh ... -R kitebuggy/rasp-cambridge`.
- `gh run view --log | grep` matches the echoed script source as well as
  output - grep for *interpolated* text (a real date, a real age in
  minutes).
