# AGENTS.md — Agent Guide for data-monitoring

Single canonical guide for autonomous coding agents in this repo. `CLAUDE.md` (and
any future `GEMINI.md`) only point here. For human-facing setup, read @README.md.

## What this repo is

`data-monitoring` is the **observability** stage of the **Agent BBB** ecosystem
(roster: `tmdb-front/doc/related-repositories/related-repositories.txt`). It does
**not** gather or transform data. It reads the finished `T_WC_*` tables and produces
nightly **coverage / progress reports** for long-running backfill campaigns — e.g.
the multi-month TV season/episode gather driven by `tmdb-crawler`.

It deliberately sits **outside** the entity pipeline described in
`tmdb-front/doc/related-repositories/groups-multi-repo-management.md` (that doc is a
T2S *entity* template; this tool is orthogonal and is intentionally absent from it).

Distinct from `html/back/srvvar.php` / `T_WC_SERVER_VARIABLE`, which tracks **realtime
process liveness**. This repo tracks **campaign trend + completion + ETA** instead.

## How it works

1. Each report is a manifest `reports/<slug>.yaml` — a bundle of metrics, each with
   read-only `done_sql` / `expected_sql` (and an optional `trend_sql` for a per-day
   rate sparkline). A new campaign (TMDb, Wikidata, anything) is a **new manifest
   file, not new code** — the design is source-agnostic.
2. `data-monitoring.py` runs the SQL, upserts one row per metric per day into
   `T_WC_DATA_MONITORING_SNAPSHOT` (idempotent), renders a self-contained HTML
   artifact `<slug>-YYYYMMDD.html` (+ daily `index` and `index-latest.html`) into
   `OUTPUT_DIR` (`/shared`), and prunes HTML older than `RETENTION_DAYS`.
3. `render.py` builds the HTML: inline CSS + inline SVG only (no CDN/JS), so reports
   open offline and archive cleanly.

## Database access (least privilege)

The `monitoring_ro` user is **read-only on the whole schema** and holds **INSERT +
UPDATE only** on `T_WC_DATA_MONITORING_SNAPSHOT` (the UPDATE serves the same-day
idempotent re-run). **No DELETE** — snapshot history is never pruned; it is the
trend. Mirrors the fastapi-text2sql pattern (read-only DB + write-only cache table).
DDL: `doc/sql/T_WC_DATA_MONITORING_SNAPSHOT.sql` (run once by a CREATE-privileged
user). All manifest SQL **must stay read-only**.

## Deployment

Dockerized, like the siblings: `python:3.10.5-slim-buster`, `requirements.txt`,
`CMD python ./data-monitoring.py`. Launched by the host crontab via
`data-monitoring.sh` (a **one-shot** container — builds, runs, exits; not a daemon).
Cron slot ~**06:30 Paris** (06:00 is taken by selenium-tmdb), output redirected to
`cron.log` so cron does not email every run. Output dir `shared_data/data-monitoring/`
is mirrored to the NAS by `sync_vps_docker.py` before the 30-day prune.

## Conventions

- **Add a tracking query with every new feature** — the ecosystem rule (see
  tmdb-crawler `AGENTS.md`). Here that means: a new campaign/column to watch = a new
  metric in a manifest. Comment what a NULL/zero means (usually "upstream has no
  value", not a gathering miss).
- SQL naming follows the ecosystem (`T_WC_*`, `DAT_*`, `TIM_*`, `*_COUNT`, …).
- Keep files UTF-8.
- Test offline with `python data-monitoring.py --sample` (no DB, writes
  `samples/`); validate the live path on the VPS where the DB is reachable.

**Last Updated**: 2026-06-23

## Backlog (Nestor second-brain)

The implementation backlog for the Agent BBB ecosystem lives in the **Nestor** knowledge repo
(a separate repo, not cloned alongside this one). This repo has no dedicated backlog file yet;
use the cross-repo dashboard:

- Dashboard: `C:\Users\vaugo\Nestor\projets\t2s-backlog\index.md`

NOTE: this is a local path on Philippe's PC and does not resolve on the VPS or on cloud agents.
