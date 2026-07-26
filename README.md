# data-monitoring

Nightly **coverage / progress reports** for long-running backfill campaigns in the
Agent BBB ecosystem — e.g. the multi-month TMDb TV season/episode gather. It reads
the finished `T_WC_*` tables (read-only), records a daily snapshot, and renders a
self-contained HTML artifact. It does **not** gather or transform data.

This answers *"how far along is the campaign, how fast is it going, when will it
finish"* — distinct from `tmdb-front`'s `srvvar.php`, which tracks realtime process
liveness.

## How it works

- A **report** is a manifest `reports/<slug>.yaml`: a bundle of metrics, each with a
  read-only `done_sql` (numerator) and `expected_sql` (denominator), plus an optional
  `trend_sql` (per-day counts → a rate sparkline), `rate_label` (the unit printed next
  to the daily rate) and a manifest-level `params:` map whose keys can be written as
  `{name}` placeholders inside the SQL (one place to change a campaign cutoff date).
  Adding a campaign — TMDb, Wikidata, anything — is a **new manifest file, not new
  code**.
- A metric may set `kind: alert_zero` instead of the default coverage shape: its
  `done_sql` returns a count that must always be 0 (a regression-guard invariant), no
  `expected_sql` needed. The report is healthy at 0 and raises a page-top alert banner
  the moment any such count exceeds 0.
- A metric may set `kind: pipeline` to track a multi-step batch job: it reads one
  `T_WC_SERVER_VARIABLE` row per step (written by the job's orchestrator) and renders a
  step timeline (done / running / pending / failed, with per-step durations). No SQL;
  configure `var_prefix` and a `steps:` list of `{code, label}`.
- `data-monitoring.py` runs the SQL, upserts one row per metric per day into
  `T_WC_DATA_MONITORING_SNAPSHOT` (idempotent), renders `<slug>-YYYYMMDD.html`
  (+ `index-YYYYMMDD.html` and `index-latest.html`) into `OUTPUT_DIR`, and prunes
  HTML older than `RETENTION_DAYS`.
- `render.py` produces HTML with inline CSS + inline SVG only — no CDN/JS, opens
  offline, archives cleanly.

## Setup

1. Copy `.env.example` → `.env` and fill DB credentials (the `monitoring_ro` user).
2. Create the history table once, with a CREATE-privileged user:
   ```sh
   mysql your_db < doc/sql/T_WC_DATA_MONITORING_SNAPSHOT.sql
   ```
3. Create the read-only user (see **Database user** below).

## Run

```sh
python data-monitoring.py                      # all reports in reports/
python data-monitoring.py --report tmdb-tv-coverage
python data-monitoring.py --sample             # seeded samples → samples/, no DB
python data-monitoring.py --print-sql          # resolved SQL of every metric, no DB
```

`--print-sql` is the pre-flight check for a new manifest: it substitutes `params:`,
prints each query, and flags two metrics that would collide on the snapshot unique
key `(DAT_CREAT, SOURCE_DB, TABLE_NAME, METRIC_KEY)`.

## Database user (least privilege)

`monitoring_ro` is read-only on the whole schema and may only write the one history
table (INSERT + UPDATE for the idempotent daily upsert — **no DELETE**, history is
never pruned):

```sql
CREATE USER 'monitoring_ro'@'%' IDENTIFIED BY 'CHANGE_ME_strong_password';
GRANT SELECT ON `your_db`.* TO 'monitoring_ro'@'%';
GRANT INSERT, UPDATE ON `your_db`.`T_WC_DATA_MONITORING_SNAPSHOT` TO 'monitoring_ro'@'%';
FLUSH PRIVILEGES;
```

## Deployment (VPS, Docker, cron)

One-shot container (builds, runs, exits — not a daemon), launched by `data-monitoring.sh`
from the host crontab at ~06:30 Paris, output redirected to a log so cron does not
email each run:

```cron
30 6 * * * /home/debian/docker/data-monitoring/data-monitoring.sh >> /home/debian/docker/data-monitoring/cron.log 2>&1
```

Output dir `shared_data/data-monitoring/` is mirrored to the NAS by
`sync_vps_docker.py` before the 30-day prune, so expiring HTML is archived first.

## Reports

### `tmdb-tv-coverage` (V1)

Four metrics: episode + season `*_series_completion` (the `TIM_*_COMPLETED` flags the
crawler stamps) and episode + season `*_volume_fill` (rows stored vs TMDb's reported
totals).

### `wikipedia-sections-refresh` + `wikipedia-sections-refresh-by-type`

Freshness of the stored Wikipedia content after `wikipedia-crawler`'s **fine-section
split** (WIKIPEDIA-CRAWLER-016: sections cut on H2 **and** H3, H3 rows carrying a
composite `Parent - Child` title), live since **2026-07-20 23:00 Paris**. Every
Wikidata ID last crawled before that timestamp still holds the old coarse H2-only
sections, so the campaign question is *how much of the base has been re-crawled since
the switch, and how fast*.

- **Denominator** = every `ID_WIKIDATA` present in `T_WC_WIKIPEDIA_PAGE_LANG` (the
  crawler's own page ledger, ~1.1M rows, all `ITEM_TYPE`s and both languages). An
  entity never crawled at all is in neither numerator nor denominator; the manifest
  ships a commented-out `wikipedia_universe_reached` metric that UNIONs the crawler's
  ~20 source tables if that blind spot ever matters (time it first).
- **Global report** — six metrics: entities re-crawled, entities re-crawled in *every*
  language, page rows re-crawled, entities whose crawl actually *succeeded*
  (`LAST_SUCCESS_AT`, the early warning for a running-but-failing re-crawl), entities
  whose section rows were actually *rewritten* (the content metric,
  `T_WC_WIKIPEDIA_PAGE_LANG_SECTION.TIM_UPDATED`), and the share of freshly written
  sections carrying an H3 composite title — the signature proving the deployed image
  really is the post-016 build.
- **By-type report** — the same coverage per `ITEM_TYPE`, ordered like the crawler's
  own priority list (`arrquickprocessids`, stalest family first). The crawler walks one
  family at a time, so a family sits near 0% until its process runs and then jumps to
  ~100%; the global percentage alone hides that.

A zero in these reports never means "upstream has no value" — it means the crawler has
not reached that slice yet.

### `tmdb-neighbours-backfill`

Progress of the TMDb **similar / recommendations** backfill (`tmdb-crawler`
TMDB-CRAWLER-022 for movies, -023 for series), which fills four tables
(`T_WC_TMDB_{MOVIE,SERIE}_{SIMILAR,RECOMMENDATION}`), live since **2026-07-07 ~16:00
Paris**. The neighbours are fetched inside the crawler's *full* entity re-crawl
(`f_tmdbmovietosqleverything` / `…serie…`), which is driven by the ~30-day refresh
loop, so the backfill completes once every movie and series has cycled through that
loop since the feature shipped.

Two angles, in order:

- **Re-crawl progress** (`movie_recrawl_progress`, `serie_recrawl_progress`) — the
  completion + ETA story. Non-deleted entities with `TIM_UPDATED` at or after the
  cutoff, over all non-deleted entities. `f_sqlupdatearray` always rewrites
  `TIM_UPDATED` on a full crawl, so this counts an entity as done even when TMDb
  returned no neighbour for it. **This is the metric to read for completion.**
- **Table fill** (`*_similar_fill`, `*_recommendation_fill`) — the direct view of the
  four tables: distinct source entities that now carry at least one stored neighbour.
  It **cannot reach 100%** — TMDb has no neighbour set for many obscure or very recent
  titles, which are re-crawled but write no row. A fill metric plateauing below the
  re-crawl metric means "these titles have no neighbours upstream", not a crawler miss.

The tables are brand new (created 2026-07-07), so every row already dates from the
backfill; the fill counts need no cutoff filter.

### `tmdb-company-wikidata`

Progress and quality of the TMDb **company → Wikidata** backfill (`tmdb-movie-preprocess`
Process 63, TMDB-MOVIE-PREPROCESS-015). A long **rolling** campaign: each run searches
at most 3000 companies (`ORDER BY TIM_WIKIPEDIA_SEARCH ASC`, never-searched first), and
the real bottleneck is resolution, not throughput — so it runs for a long time and never
trivially reaches 100%.

State per eligible company (non-empty `NAME`, not deleted) in `T_WC_TMDB_COMPANY`: a
match writes `ID_WIKIDATA` + `CONFIDENCE` + `TIM_WIKIPEDIA_SEARCH`; a miss writes only
`TIM_WIKIPEDIA_SEARCH`; generic/brand-collision-risk matches are quarantined at the 0.50
`CONFIDENCE` sentinel (usable floor is 0.9). Five metrics, all framed so **higher is
better** (the bar-colour code reds out *below* a threshold):

- `company_wikidata_attempted` — frontier coverage (searched / eligible), the ETA metric.
- `company_wikidata_usable` — usable links (`CONFIDENCE >= 0.9`) / eligible.
- `company_wikidata_linked` — any link / eligible; the gap with *usable* is the quarantine backlog.
- `company_wikidata_match_rate` — links / searched: the direct read on the resolution bottleneck.
- `company_wikidata_clean_link_share` — usable / linked: the inverse of the quarantine backlog.

No cutoff (it is a current-state coverage snapshot). Low outcome values are not a crawler
miss — most small companies simply have no Wikidata entity. When the "Wikidata ID
everywhere" epic wires network / genre / character, clone this manifest per entity.

### `tmdb-poster-invariants`

A **regression guard**, not a coverage report. `DISPLAY_ORDER 0` is reserved for the
canonical en/'' poster, so no localized French poster may sit there;
tmdb-crawler TMDB-CRAWLER-024/025/026 closed every writer that could clamp one and
backfilled the old rows. The two metrics count French posters still at position 0 in
`T_WC_TMDB_MOVIE_IMAGE` / `T_WC_TMDB_SERIE_IMAGE`; both must be **0 forever**. A non-zero
value is a tmdb-crawler regression, not a data gap.

These use the `kind: alert_zero` metric type: the value is healthy at 0 and raises a
page-top **ALERT banner** (plus a red card) the instant it exceeds 0. No denominator, no
percentage — the sparkline is the raw count history and should be a flat line on zero.

### `wikidata-etl-pipeline`

Step-by-step progress of the **multi-day** Wikidata dump ingestion (`wikidata-crawler`,
steps 101-114: download the ~90 GB `.bz2`, three streaming passes — pass1 / pass2 /
item_cache — then staging load, target bulk-load, media resolution, cleanup). The early
passes write **files** on `/shared`, invisible to a MariaDB query, but the orchestrator
also writes one `T_WC_SERVER_VARIABLE` row per step
(`strwikidatacrawlerstep<code>{status,startedat,finishedat}`). This report uses the
`kind: pipeline` metric type to read those and render a **step timeline** — each step as
done / running / pending / failed with its start→finish and duration, an overall
status/current-step header, a steps-completed bar, and a daily completion trend. A failed
run raises the page-top alert banner with the last error.

Cadence note: data-monitoring runs once a day (~06:30), so this is a daily **checkpoint**
of a days-long job, not a live console — for real-time step status use tmdb-front's
`srvvar.php`, which reads the same server variables live. Cost is one indexed
`LIKE 'strwikidatacrawler%'` scan of the small `T_WC_SERVER_VARIABLE` table.
