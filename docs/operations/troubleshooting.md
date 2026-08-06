# Troubleshooting

Failure modes seen (or anticipated) in this system, most operational first.
When investigating a bad run, start from the Actions log and
`public/build_state.json` - by design, calendar entries never carry
diagnostics.

### Issue: `NOT A CHART` / build_state has `"status": "intercepted"` / "not a PNG - ... HTTP 200 ... <!DOCTYPE html>"

**Cause:** something in front of RASP intercepted the request - a WAF,
bot-protection interstitial or host error page, typically with a misleading
HTTP 200. It is NOT RASP's own script erroring: `blip_stars.php` has no
HTML error path (verified 2026-07-29 - bogus model, nonsense coords and
no-params all return PNGs). Evidence points to an intermittent per-IP block
on Actions runner IPs: on 2026-07-29 two Actions runs got HTML for all 7
models while a direct fetch from another machine minutes later got a valid
PNG, and six different User-Agents all worked from the good IP. Observed
rate ≈ 2 in 16 runs. Same failure occurred 2026-07-28 under the old
workflow, so it was not caused by any recent change.

**Solution:** usually nothing - each Actions job gets a fresh runner IP, so
the next scheduled run is the effective retry (in-run retries would not
help a per-job IP block). If it recurs, read the intercept page's `<title>`
from the Actions log (`_describe_non_png` prints status, content-type,
server, title and body sample to stderr). If it names
Cloudflare/Wordfence/a host block page, the durable fix is contacting the
RASP admin (Paul Scorer, stratus.org.uk) - and consider whether our request
rate is contributing before increasing it.

### Issue: `STALE: chart Last-Modified <date> is from before today` / `"status": "stale"`

**Cause:** RASP has not yet regenerated today's run for that model; it is
still serving yesterday's file. Normal in the early morning and for
long-range UK12 days.

**Solution:** none needed - this is the freshness guard working. The day is
published as a placeholder and a later run picks up the regenerated chart.
Do NOT weaken the `Last-Modified` check or fall back to the stale chart
(AI_README Critical rule 1).

### Issue: `Implausible x-tick count N (expected 12..32)` or other parse failure

**Cause:** either RASP served one of its small error charts (bogus request,
partial regen) - which this guard exists to reject - or the chart template
has changed (size, axis geometry, or the `(238,130,238)` line colour).

**Solution:** open the audit PNG for that day under `public/charts/` (or
the saved file in the Actions artifact). A tiny ~2-3 KB chart = RASP-side,
retry handles it. A normal-looking chart that fails = template drift;
update the constants in `rasp_stars.py` (`LINE_RGB`, `N_SLOTS_*`,
geometry assumptions in `_find_ticks`) and consider finally adding the
reference-PNG regression test.

### Issue: no runs this morning / calendar not refreshing

**Cause candidates, in order:** GitHub dropped the early slots (measured:
05:xx/06:xx have the worst miss rate - see
[`scheduling.md`](scheduling.md)); the workflow was auto-disabled after 60
days of repo inactivity (banner in the Actions tab); a schedule edit is
sitting on a non-default branch; or the decide step is correctly no-opping
because the published data is fresh and complete.

**Solution:** check the Actions run list for decide-step summary lines
("Rebuild: false - data is 47m old and complete..." is healthy). A wholly
quiet morning usually self-heals at the next dispatched slot; the max-age
rule forces a rebuild once data ages past `MAX_AGE_MIN`. For guaranteed
timing, see the external-scheduler note in `scheduling.md`.

### Issue: Jason's local `git add`/`commit` dies with "Unable to create index.lock: File exists"

**Cause:** a sandboxed AI session ran a bare `git status`/`git diff` in the
mounted repo. Those refresh the index via a lockfile; the mount denies the
unlink, so a zero-byte `.git/index.lock` is left behind (happened
2026-08-03).

**Solution:** `rm -f .git/index.lock` (Jason, locally). Prevention:
AI sessions prefix every read-only git command with `GIT_OPTIONAL_LOCKS=0`
and check `ls .git/*.lock` afterwards; modifying git is Jason-only
(AI_README Critical rule 5).

### Issue: chart image missing in Outlook / Google Calendar

**Cause:** expected client behaviour, not a bug. Outlook strips `data:`
URIs in some configurations; Google strips `ATTACH` and `X-ALT-DESC`
entirely and falls back to the plain-text `DESCRIPTION`.

**Solution:** none - the plain-text body (with sparkline) always renders,
and the chart PNGs are linkable at `/charts/<date>_<model>.png`.

### Issue: decide step prints Python tracebacks

**Cause:** corrupt or truncated `build_state.json` - each field is read
with `|| echo` fallbacks, so every parse error prints a traceback and then
resolves to the safe default (`build=true`).

**Solution:** none needed; the run rebuilds and rewrites clean state. The
tracebacks are left in deliberately - they only appear when you would want
them.

### Issue: workflow deprecation warnings about Node versions

**Cause:** an action pin has fallen behind a runtime transition (this is
how the Node 20 deprecation was noticed).

**Solution:** let Dependabot's weekly PR bump the SHA pins, or resolve
manually with `git ls-remote --tags --refs https://github.com/actions/<repo>`
(api.github.com is blocked from the sandbox); verify the target tag's
`using:` and inputs before pinning. Never set
`ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION`; `upload-pages-artifact` must
stay ≥ v5 (see AI_README Critical rule 7).
