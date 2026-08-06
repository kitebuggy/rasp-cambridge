# Documentation index - rasp_cambridge

Start at the root [`AI_README.md`](../AI_README.md) for the rules that must
not be broken. The human-facing overview (what this is, how to subscribe,
how to fork it) is the root [`README.md`](../README.md).

## Architecture

- [`architecture/pipeline.md`](architecture/pipeline.md) - the data
  pipeline: chart fetch, pixel parsing, fly-score calculation, ICS
  generation, placeholders and the build-state file.
- [`architecture/publishing.md`](architecture/publishing.md) - how the
  calendar reaches subscribers (GitHub Pages serves the Actions artifact,
  not git) and what is and is not committed, with the history-bloat numbers
  behind the policy.

## Operations

- [`operations/scheduling.md`](operations/scheduling.md) - the one-cron +
  max-age design, the measurements showing GitHub cron is lossy, and how to
  diagnose scheduling problems without misreading the run list.
- [`operations/troubleshooting.md`](operations/troubleshooting.md) -
  known failure modes and what to do about them: WAF intercepts, stale
  charts, parse failures, quiet mornings, stale git locks.

## Reference

- [`reference/filesystem-map.md`](reference/filesystem-map.md) - annotated
  directory tree. Hand-maintained; update it when files move.
- [`reference/dependency-map.md`](reference/dependency-map.md) - external
  dependencies, internal call graph, artefact flow and the couplings between
  script, workflow and `.gitignore`. Hand-maintained.

## Conventions for these docs

- Documentation lives in `docs/`; nothing stray at the repo root beyond
  `README.md`, `AI_README.md` and `LICENSE`.
- Record the *why*, not just the *what* - most of this repo's complexity is
  decisions (freshness rule, artifact publishing, max-age scheduling), and
  the docs exist so those decisions are not accidentally reversed.
- The workflow file's comments are documentation too. When behaviour and
  comments diverge, fix the comments in the same change.
- Dates matter: measurements and incidents are cited with their dates so a
  future reader can judge whether they still apply.
