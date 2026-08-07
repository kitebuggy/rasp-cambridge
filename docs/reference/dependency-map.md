# Dependency map

Hand-maintained (no generator script in this repo, unlike the Jekyll
sites). Update after changes to `rasp_stars.py`, the workflow, or
`.gitignore` - the couplings section is the part that prevents accidents.

## External dependencies

| Dependency | Used by | Notes |
|------------|---------|-------|
| `app.stratus.org.uk/blip/graph/blip_stars.php` | `fetch_png` | The ONLY data source. No API, no fallback. Volunteer-run (Paul Scorer). Sits behind something that intermittently intercepts Actions IPs - see troubleshooting. |
| `numpy` (>=1.26, floating) | parsing + summarising | |
| `Pillow` (>=10.0, floating) | PNG decode | A decoding change could silently shift ratings - accepted risk, no tests. |
| Python stdlib | throughout | urllib, argparse, base64, csv, datetime, io, json, dataclasses, pathlib |
| GitHub Actions scheduler | workflow | Lossy - see `../operations/scheduling.md` |
| GitHub Pages | deploy job | Serves the artifact, not git |
| Calendar clients (Apple / Outlook / Google) | consumers | Feature support differs - see pipeline.md ICS section |

## CI dependencies (SHA-pinned, Dependabot-maintained)

| Action | Pin policy |
|--------|-----------|
| `actions/checkout` | Full SHA + version comment. Commit step depends on its default `persist-credentials: true`. |
| `actions/setup-python` | Full SHA. `python-version: "3.14"` (minor only, patches auto). |
| `actions/upload-pages-artifact` | Full SHA. Must stay ≥ v5 (earlier majors pin node20 upload-artifact). |
| `actions/deploy-pages` | Full SHA. |

`.github/dependabot.yml` (github-actions ecosystem, weekly) keeps pins
current; without it SHA pinning means running stale code forever.

## Internal call graph (rasp_stars.py)

```
__main__ (argparse)
└── run(location, lat, lon, out_dir, ics, state)
    ├── fetch_png(model, lat, lon)                 per day, 7×
    │   ├── raises StaleChartError                 Last-Modified < today UTC
    │   ├── raises NotAChartError                  body not \x89PNG
    │   │   └── _describe_non_png                  full dump → stderr, short form → reason
    │   └── returns PNG bytes
    ├── parse_stars_png(png)
    │   └── _find_ticks(arr)                       axis + tick detection, slot-count guard
    ├── summarise(date, model, slots) → DaySummary fly score, XC window, metrics
    │   (any failure above → DayFailure(kind: stale|intercepted|fetch|parse))
    ├── write_csv → halfhour.csv
    ├── write_summary_csv → summary.csv
    ├── write_ics(summaries, failures)
    │   ├── _build_description                     plain text; _spark, _xc_hours
    │   ├── _build_html                            X-ALT-DESC table + data: image
    │   ├── _build_placeholder_description         NO diagnostics (rule 2)
    │   ├── _build_placeholder_html
    │   ├── _ics_escape
    │   └── _fold_ics_line                         RFC 5545 75-octet folding, UTF-8-safe
    └── write_state → build_state.json             read by next run's decide step
```

## Artefact flow

```
rasp_stars.py writes                    consumed by
────────────────────                    ───────────
public/cambridge_rasp.ics           →   calendar clients (via Pages artifact)
public/charts/<date>_<model>.png    →   humans auditing parser drift (via Pages)
public/charts/{halfhour,summary}.csv →  humans; git history = long-term audit trail
public/build_state.json             →   NEXT workflow run's decide step (via git)
stderr                              →   Actions log (diagnostics live here)
public/index.html (workflow heredoc) →  humans landing on the Pages site
```

## Couplings that are easy to break

- **`--state ./public/build_state.json` (workflow "Generate calendar" step)
  ↔ `STATE: public/build_state.json` (decide step env).** Rename one, the
  decide step never sees state again and every run rebuilds.
- **Decide step ↔ state-file schema.** It reads `run_date`, `complete`,
  `days_failed`, `generated_utc` (ISO-8601 `...Z`). Changing `write_state`'s
  field names or timestamp format silently degrades to always-rebuild
  (safe direction, but wasteful and easy to miss).
- **Commit step's `git add public/` ↔ `.gitignore`.** The ignore rules are
  the only thing keeping the ICS and chart PNGs out of git.
- **Published site ↔ next build.** The workflow passes
  `--previous-base https://<owner>.github.io/<repo>`, so the deployed
  artifact is an *input* to the following run (`load_published_state`,
  `fetch_published_chart`). Breaking the deploy breaks carry-forward;
  removing charts from the artifact silently disables it.
- **Prune step ↔ freshness rule.** `rm -rf public/charts`, `rm -f
  public/cambridge_rasp.ics` before each build is deliberate (no
  inheritance of stale output); build_state.json is exempt because the next
  run must read it.
- **`LINE_RGB`, `_find_ticks` geometry, `N_SLOTS_MIN/MAX` ↔ RASP's chart
  template.** Not under our control; drift shows up as parse failures.
- **`DAY_MODELS` ↔ RASP's day-index → model mapping** (from the RASP page
  JS). UID scheme is date-based, so model changes do not orphan events.
- **checkout pin ↔ `persist-credentials: true` default** - the commit
  step's `git push` depends on it.
- **`deploy` job's `if: needs.build.outputs.rebuilt == 'true'` ↔ decide
  step's `build` output** via the `rebuilt` job output.
- **`RETRY_NOTE_DEFAULT` (script) and index.html blurb (workflow) ↔ the
  schedule.** Both deliberately avoid quoting exact times so schedule edits
  do not require prose edits - keep it that way.
