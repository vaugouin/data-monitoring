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
  `trend_sql` (per-day counts → a rate sparkline). Adding a campaign — TMDb, Wikidata,
  anything — is a **new manifest file, not new code**.
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
python data-monitoring.py --sample             # seeded sample → samples/, no DB
```

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

## V1 scope

One report, `tmdb-tv-coverage`, with four metrics: episode + season
`*_series_completion` (the `TIM_*_COMPLETED` flags the crawler stamps) and episode +
season `*_volume_fill` (rows stored vs TMDb's reported totals).
