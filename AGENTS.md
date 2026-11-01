# AGENTS.md - Agent Guide for data-monitoring

## Canary

Start every reply with "Philippe,": no exceptions, including short replies and tool summaries.

Single canonical guide for autonomous coding agents in this repo. `CLAUDE.md` (and
any future `GEMINI.md`) only point here. What the tool does, how it runs, the manifest
format, setup, deployment and every report: @README.md. Each manifest's header
comments carry its modelling details and caveats.

## Scope

Observability stage of the Agent BBB ecosystem (roster:
`%USERPROFILE%/Nestor/projets/t2s-backlog/topics/related-repositories.txt`): campaign
trend, completion and ETA, never realtime liveness (that is `srvvar.php`), plus the
daily ops dashboard of the automated tasks (a morning checkpoint, not a live console). It is
deliberately absent from the entity pipeline template
`%USERPROFILE%/Nestor/projets/t2s-backlog/topics/groups-multi-repo-management.md`.

## Ops dashboard (`ops.py`, since 2026-10-31)

The morning health page of every automated task (README § Ops dashboard). Pieces:
`collect_host.py` (runs on the HOST, stdlib only, Python 3.10-compatible), `ops.py`
(verdicts), `render_ops.py` (HTML), `ops/tasks.yaml` (registry), `ops/extra-logs.txt`.

- A new cron job, launcher or always-on container on the VPS gets an entry in
  `ops/tasks.yaml` in the same change (same rule as a tracking metric for a new feature).
- A job that changes its server-variable names, container name, cron line or log path
  breaks its verdict silently into UNKNOWN or STALE: update the registry with it.
- The ops dashboard must never take the coverage reports down: `run_ops` is wrapped in
  `main()`; keep it that way.
- Log tails are shown on a password-protected page: `collect_host.py` masks secrets
  (`_mask`). Never add a probe that dumps an `.env` or a container's environment.
- Test offline with a fake connection and a synthetic `host.json` (no `--sample` mode
  for ops yet); validate the host side on the VPS with `./data-monitoring.sh --ops-only`.

## Rules

- A new campaign or guard is a **new manifest file, not new code**. Every new feature
  elsewhere in the ecosystem comes with a tracking metric here (rule from tmdb-crawler
  `AGENTS.md`); comment what a NULL/zero means (usually "upstream has no value").
- All manifest SQL **must stay read-only**. `monitoring_ro` may only INSERT/UPDATE the
  snapshot table, never DELETE: snapshot history is the trend and is never pruned.
- `post_run:` is the routine of every run. A one-off check for one run goes above it,
  dated, and comes out once done, otherwise the next run repeats it. It is not an alert
  banner on purpose: a to-do is not a breached invariant.
- A `pipeline` metric's step count is `len(steps)`, never a literal.
- Pages already generated cannot be faithfully regenerated (step state comes from
  overwritten server variables): retrofit them with `backfill_day_nav.py`, never by
  re-rendering.
- Test offline with `--sample` and `--print-sql` (no DB); validate the live path on the VPS.
- SQL naming follows the ecosystem (`T_WC_*`, `DAT_*`, `TIM_*`, `*_COUNT`); files UTF-8.

## Reports in place

The list and the meaning of each report: README.md § Reports. `wikidata-etl-pipeline`
mirrors `wikidata-crawler`'s steps (101-115) and `T_WC_SERVER_VARIABLE` markers (last
synced 2026-09-20): when the crawler gains a step or a marker, this manifest has to follow.

Traps when touching manifests:

- **The snapshot unique key does NOT include `REPORT_SLUG`**: it is
  `(DAT_CREAT, SOURCE_DB, TABLE_NAME, METRIC_KEY)`. Two reports on the same table must
  not reuse a metric key, or their daily rows silently overwrite each other.
  `--print-sql` flags the collision.
- **`T_WC_WIKIPEDIA_PAGE_LANG_SECTION` is huge** (~170M rows). Only ever filter it on
  the indexed `TIM_UPDATED` range; take denominators from the small
  `T_WC_WIKIPEDIA_PAGE_LANG` (~1.1M rows). On the link table, probe with a correlated
  `LIMIT 1`, not `EXISTS` (MariaDB turns it into a full semi-join; see the
  `wikipedia-content-md-backfill` manifest header).
- **Release-date and watch-provider child tables are authoritative snapshots**
  (delete + reinsert per title; a valid empty response leaves no row but stamps the
  parent). Use `TIM_RELEASE_DATES_COMPLETED` / `TIM_WATCH_PROVIDERS_COMPLETED` for
  coverage, never derive a gather curve from child `DAT_CREAT`, and set
  `show_eta: false` on their non-monotonic fill/freshness metrics.
- Querying a column that may not exist yet takes the whole report down at parse time:
  check existence through `information_schema` (as `wikidata_aliases_ddl` does).

## Clock rule (DEPLOYMENT-015, since 2026-10-19)

Every human-read date is **Europe/Paris with a numeric offset** (`2026-10-19 08:30
+0200`, `%z` or `isoformat()`, never `%Z`/`CEST` and never a fixed `+02:00`:
`USER_TIMEZONE` in `_now()`). Naive `DATETIME` values written to the DB stay **naive
Paris**. The VPS host stays `Etc/UTC`, so cron hours are UTC. Pages generated before
2026-10-19 print `CEST`/`CET`. Manifest SQL comparing `NOW()` with Paris columns is exact
only once the MariaDB server clock is on Paris (TMDB-FRONT-100).

## Backlog (Nestor second-brain)

No dedicated backlog file; use the cross-repo dashboard
`C:\Users\vaugo\Nestor\projets\t2s-backlog\index.md` (local path on Philippe's PC, absent
on the VPS and on cloud agents).
