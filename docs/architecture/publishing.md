# Publishing model

## The one fact everything follows from

**Subscribers are served from the GitHub Pages *artifact*, not from git.**
`actions/upload-pages-artifact` tars `public/` from the runner's working
tree; `actions/deploy-pages` publishes that tarball. Git tracking is
irrelevant to what subscribers fetch - a file can be gitignored and still
deployed, linkable, and up to date.

```
build job (runner working tree)
  rasp_stars.py ──► public/{cambridge_rasp.ics, charts/*.png, *.csv,
        │                    build_state.json}  + generated index.html
        │
        ├── git commit ──► ONLY build_state.json, CSVs, index.html
        │                  (ICS + chart PNGs are gitignored)
        │
        └── upload-pages-artifact (tars ALL of public/)
                 │
                 ▼
        deploy job ──► GitHub Pages ──► calendar clients poll
                       https://kitebuggy.github.io/rasp-cambridge/cambridge_rasp.ics
```

A skipped build uploads no artifact, so there is nothing to deploy and the
previous Pages deployment simply stays live - which is why a no-op decide
result is safe.

## What is committed, and why

| Path | Committed? | Why |
|------|-----------|-----|
| `public/build_state.json` | yes | The decide step on the *next* run must read it - it is the only cross-run persistence in the system. Also keeps the repo active (see the 60-day auto-disable in `../operations/scheduling.md`). |
| `public/charts/halfhour.csv`, `summary.csv` | yes | Small, delta well, and are the surviving audit trail (historical star readings are reconstructable from CSV history). |
| `public/index.html` | yes (workflow-generated) | Small; regenerated each build from the heredoc in the workflow. |
| `public/cambridge_rasp.ics` | **no** (gitignored) | Embeds every chart PNG as base64, so it changes wholesale every run and never deltas. |
| `public/charts/*.png` | **no** (gitignored) | Rebuilt every run; binary; still deployed and linkable at `/charts/<date>_<model>.png`. |

## The history-bloat numbers behind the policy

Measured 2026-07-29 on a clone ~2.5 months stale: 24 tracked ICS versions =
10.7 MB uncompressed plus 3.2 MB of chart PNGs in a 3.5 MB pack - roughly
**500 KB of permanent history per rebuild, at 3+ rebuilds/day**. Jason had
twice removed the charts manually ("Removed charts", "Removed charts2") and
the workflow's `git add public/` silently re-added them on the next run;
the durable fix was `.gitignore`, not manual removal. The pre-existing ICS
blobs were later stripped from history with `git-filter-repo` (the CSVs
were kept, hence "surviving audit trail" above).

**Never weaken `.gitignore` for `public/`** - the commit step's blanket
`git add public/` depends on it (AI_README Critical rule 3).

## Deployment mechanics

- Pages must be configured *Settings → Pages → Source: GitHub Actions*
  (done for this repo; needed again on any fork).
- The `build` job commits as `github-actions[bot]` and pushes; this relies
  on `actions/checkout` defaulting `persist-credentials: true` - check that
  before bumping the checkout pin.
- Permissions are least-privilege per job: workflow-level `contents: read`;
  `build` adds `contents: write`; `deploy` gets `pages` + `id-token` only.
- Both jobs have `timeout-minutes: 5`; a `concurrency` group
  (`build-rasp`, no cancel-in-progress) stops two slow scheduled runs
  racing the commit step.
