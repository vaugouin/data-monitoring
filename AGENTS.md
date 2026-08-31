# AGENTS.md - Agent Guide for data-monitoring

Single canonical guide for autonomous coding agents in this repo. `CLAUDE.md` (and
any future `GEMINI.md`) only point here. For human-facing setup, read @README.md.

## What this repo is

`data-monitoring` is the **observability** stage of the **Agent BBB** ecosystem
(roster: `%USERPROFILE%/Nestor/projets/t2s-backlog/topics/related-repositories.txt`). It does
**not** gather or transform data. It reads the finished `T_WC_*` tables and produces
nightly **coverage / progress reports** for long-running backfill campaigns - e.g.
the multi-month TV season/episode gather driven by `tmdb-crawler`.

It deliberately sits **outside** the entity pipeline described in
`%USERPROFILE%/Nestor/projets/t2s-backlog/topics/groups-multi-repo-management.md` (that doc is a
T2S *entity* template; this tool is orthogonal and is intentionally absent from it).

Distinct from `html/back/srvvar.php` / `T_WC_SERVER_VARIABLE`, which tracks **realtime
process liveness**. This repo tracks **campaign trend + completion + ETA** instead.

## How it works

1. Each report is a manifest `reports/<slug>.yaml` - a bundle of metrics, each with
   read-only `done_sql` / `expected_sql` (and an optional `trend_sql` for a per-day
   rate sparkline, `rate_label` for the printed unit). A manifest-level `params:` map
   is substituted into the SQL via `str.format()`, so a value repeated across metrics
   (a campaign cutoff date) lives in one place. A metric may also set `kind: alert_zero`
   (default is `coverage`): its `done_sql` returns a count that must always be 0 (an
   invariant / regression guard, no `expected_sql`); `render._alert_card` shows an OK/ALERT
   card and `render._alert_banner` raises a page-top banner when the count is > 0. A third
   kind, `pipeline`, tracks a multi-step batch job: it reads per-step
   `T_WC_SERVER_VARIABLE` markers (`var_prefix` + a `steps:` list of `{code,label}`), no
   SQL, and `render._pipeline_card` draws a step timeline. A new campaign or guard is a
   **new manifest file, not new code** - the design is source-agnostic.
2. `data-monitoring.py` runs the SQL, upserts one row per metric per day into
   `T_WC_DATA_MONITORING_SNAPSHOT` (idempotent), renders a self-contained HTML
   artifact `<slug>-YYYYMMDD.html` (+ daily `index` and `index-latest.html`) into
   `OUTPUT_DIR` (`/shared`), and prunes HTML older than `RETENTION_DAYS`.
3. `render.py` builds the HTML: inline CSS + inline SVG only (no CDN/JS), so reports
   open offline and archive cleanly. Each page also carries a prev/next **day-nav** bar
   (`render._day_nav_bar`, hrefs computed in `write_artifacts._day_nav`) to walk the same
   report over time.
4. `backfill_day_nav.py` is a one-shot, idempotent maintenance tool that injects that
   nav bar into **already-generated** pages (they cannot be faithfully regenerated: a
   pipeline card's step state comes from overwritten server variables). It edits the HTML
   in place after `</header>` with an inline-styled `<nav>` and reads prev/next existence
   from the files present in the target dir. Run it on `OUTPUT_DIR` and the NAS archive.

## Database access (least privilege)

The `monitoring_ro` user is **read-only on the whole schema** and holds **INSERT +
UPDATE only** on `T_WC_DATA_MONITORING_SNAPSHOT` (the UPDATE serves the same-day
idempotent re-run). **No DELETE** - snapshot history is never pruned; it is the
trend. Mirrors the fastapi-text2sql pattern (read-only DB + write-only cache table).
DDL: `doc/sql/T_WC_DATA_MONITORING_SNAPSHOT.sql` (run once by a CREATE-privileged
user). All manifest SQL **must stay read-only**.

## Deployment

Dockerized, like the siblings: `python:3.10.5-slim-buster`, `requirements.txt`,
`CMD python ./data-monitoring.py`. Launched by the host crontab via
`data-monitoring.sh` (a **one-shot** container - builds, runs, exits; not a daemon).
Cron slot ~**06:30 Paris** (06:00 is taken by selenium-tmdb), output redirected to
`cron.log` so cron does not email every run. Output dir `shared_data/data-monitoring/`
is mirrored to the NAS by `sync_vps_docker.py` before the 30-day prune.

## Reports in place

- `tmdb-tv-coverage` - TMDb TV season/episode backfill (V1).
- `tmdb-release-dates-coverage` - Process 34 coverage, 35-day freshness and
  structural quality for the additive `T_WC_TMDB_MOVIE_RELEASE_DATE` snapshots.
  Completion comes from `T_WC_TMDB_MOVIE.TIM_RELEASE_DATES_COMPLETED`; an empty
  child snapshot is a valid result and `DAT_RELEASE` remains untouched.
- `tmdb-watch-providers-coverage` - Processes 35/36 coverage, 35-day freshness,
  current fill and attribution integrity for movie and series TMDb / JustWatch
  snapshots. Completion comes from the parent markers, not child-row presence.
- `wikipedia-sections-refresh` + `wikipedia-sections-refresh-by-type` - how much of the
  Wikidata universe has been re-crawled since `wikipedia-crawler`'s fine-section split
  (H2+H3, WIKIPEDIA-CRAWLER-016) went live on **2026-07-20 23:00 Paris**. Anything last
  crawled before that cutoff still holds coarse H2-only sections, so these two are pure
  **freshness** reports, not gathering-completeness ones. Details and caveats live in
  the manifest headers; a summary is in @README.md.
- `wikidata-etl-pipeline` - step timeline for the multi-day Wikidata dump ingestion
  (`wikidata-crawler`, steps 101-114). Uses the `kind: pipeline` metric type: it reads the
  orchestrator's per-step `T_WC_SERVER_VARIABLE` markers
  (`strwikidatacrawlerstep<code>{status,startedat,finishedat}`) rather than SQL, because
  the early passes produce files on `/shared`, not DB rows. Renders each step as
  done/running/pending/failed with durations; snapshot stores steps-done/14 for a daily
  trend; a run FAILURE hits the alert banner. Deliberately a daily checkpoint, not a live
  console (that is srvvar.php). This is the first use of the `pipeline` kind.
- `tmdb-poster-invariants` - regression guard (NOT a backfill): French posters must never
  sit at `DISPLAY_ORDER 0` (reserved for the en/'' canonical) after TMDB-CRAWLER-024/025/026.
  Two `kind: alert_zero` metrics count FR posters at 0 in `T_WC_TMDB_{MOVIE,SERIE}_IMAGE`;
  both must stay 0, and any breach raises a page-top ALERT banner + red card. This is the
  first use of the `alert_zero` metric kind (see below).
- `tmdb-company-wikidata` - progress + quality of the TMDb company→Wikidata backfill
  (`tmdb-movie-preprocess` Process 63, TMDB-MOVIE-PREPROCESS-015). A long **rolling**
  campaign (≤3000 companies/run, `TIM_WIKIPEDIA_SEARCH ASC`). Modelling: eligible universe
  = non-empty `NAME` + not deleted (the crawler's own filter, kept DRY in `params.eligible`);
  a match writes `ID_WIKIDATA`+`CONFIDENCE`+`TIM_WIKIPEDIA_SEARCH`, a miss writes only the
  timestamp, quarantine = `CONFIDENCE` 0.50 sentinel (usable floor 0.9). All five metrics
  are framed **higher-is-better** so the red-below-threshold colouring reads correctly -
  the quarantine backlog and resolution gap appear as the complement of
  `clean_link_share` / `match_rate`. No cutoff (current-state coverage, not freshness).
- `tmdb-neighbours-backfill` - progress of the TMDb similar/recommendations backfill
  (`tmdb-crawler` TMDB-CRAWLER-022/023) into `T_WC_TMDB_{MOVIE,SERIE}_{SIMILAR,RECOMMENDATION}`,
  live since **2026-07-07 ~16:00 Paris**. Key modelling choice: the neighbours are
  fetched inside the crawler's *full* re-crawl, so the true completion signal is
  `T_WC_TMDB_{MOVIE,SERIE}.TIM_UPDATED >= cutoff` (the re-crawl-progress metrics), NOT a
  count on the neighbour tables - a title re-crawled but with no TMDb neighbour writes
  no row, so the four `*_fill` metrics structurally cannot reach 100%. Both angles are in
  the manifest, re-crawl first.

Two traps when touching these manifests:

- **The snapshot unique key does NOT include `REPORT_SLUG`** - it is
  `(DAT_CREAT, SOURCE_DB, TABLE_NAME, METRIC_KEY)`. Two reports on the same table must
  not reuse a metric key, or their daily rows silently overwrite each other. Run
  `python data-monitoring.py --print-sql`: it flags the collision.
- **`T_WC_WIKIPEDIA_PAGE_LANG_SECTION` is huge** (~170M rows). Only ever filter it on
  the indexed `TIM_UPDATED` range; a `COUNT(DISTINCT ID_WIKIDATA)` over the whole table
  would scan it entirely every night. The denominators above deliberately come from
  the small `T_WC_WIKIPEDIA_PAGE_LANG` (~1.1M rows) instead.
- **Release-date and watch-provider child tables are authoritative snapshots.** A
  successful refresh deletes and reinserts a title's rows, and a valid empty response
  leaves no row while stamping the parent completion marker. Use
  `TIM_RELEASE_DATES_COMPLETED` / `TIM_WATCH_PROVIDERS_COMPLETED` for coverage; never
  derive a historical gather curve from child `DAT_CREAT`. Their fill and freshness
  metrics are non-monotonic and must set `show_eta: false`.

## Conventions

- **Add a tracking query with every new feature** - the ecosystem rule (see
  tmdb-crawler `AGENTS.md`). Here that means: a new campaign/column to watch = a new
  metric in a manifest. Comment what a NULL/zero means (usually "upstream has no
  value", not a gathering miss).
- SQL naming follows the ecosystem (`T_WC_*`, `DAT_*`, `TIM_*`, `*_COUNT`, …).
- Keep files UTF-8.
- Test offline with `python data-monitoring.py --sample` (no DB, writes
  `samples/`) and `--print-sql` (no DB, resolves `params:` and checks metric keys);
  validate the live path on the VPS where the DB is reachable.

**Last Updated**: 2026-08-19

## Backlog (Nestor second-brain)

The implementation backlog for the Agent BBB ecosystem lives in the **Nestor** knowledge repo
(a separate repo, not cloned alongside this one). This repo has no dedicated backlog file yet;
use the cross-repo dashboard:

- Dashboard: `C:\Users\vaugo\Nestor\projets\t2s-backlog\index.md`

NOTE: this is a local path on Philippe's PC and does not resolve on the VPS or on cloud agents.
