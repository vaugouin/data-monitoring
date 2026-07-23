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
