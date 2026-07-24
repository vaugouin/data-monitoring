#!/usr/bin/env python3
"""data-monitoring — nightly coverage/progress reports for long-running backfills.

For each report manifest in reports/<slug>.yaml it:
  1. runs read-only SQL against the monitored database,
  2. writes one snapshot row per metric per day to T_WC_DATA_MONITORING_SNAPSHOT
     (idempotent upsert), computing the daily rate from prior snapshots,
  3. renders a self-contained HTML artifact <slug>-YYYYMMDD.html into the
     shared_data output directory, plus a daily index and index-latest.html,
  4. prunes HTML artifacts older than RETENTION_DAYS (history stays in the table;
     the NAS sync archives the files before they expire).

Source-agnostic by design: a new campaign — TMDb, Wikidata, anything — is a new
manifest entry, not new code.

Usage:
  python data-monitoring.py                      # all reports in reports/
  python data-monitoring.py --report tmdb-tv-coverage
  python data-monitoring.py --sample             # seeded sample, no DB, no writes
"""
import argparse
import datetime
import glob
import os
import sys

import render

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/shared")
TZ = os.environ.get("USER_TIMEZONE", "Europe/Paris")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
SOURCE_DEFAULT = "DATA"


def _now():
    import pytz
    return datetime.datetime.now(pytz.timezone(TZ))


def _pct(done, expected):
    if done is None or not expected:
        return None
    return round(100.0 * float(done) / float(expected), 2)


def load_manifest(path):
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _sql(metric, key, params):
    """Metric SQL with the manifest-level `params:` substituted in.

    A manifest that declares `params:` may write `{name}` placeholders in its SQL —
    used to keep a value repeated across many metrics (a campaign cutoff date, say)
    in one place. Without `params:` the SQL is passed through untouched, so existing
    manifests keep working even if they contain braces.
    """
    sql = metric.get(key)
    if sql and params:
        sql = sql.format(**params)
    return sql


def run_report(conn, manifest, run_dt):
    """Execute every metric, persist a snapshot, return the result rows for render."""
    import db
    slug = manifest["slug"]
    source_db = manifest.get("source_db", SOURCE_DEFAULT)
    params = manifest.get("params") or {}
    dat = run_dt.date()
    tim = run_dt.strftime("%Y-%m-%d %H:%M:%S")
    results = []
    for order, m in enumerate(manifest["metrics"], start=1):
        done = db.scalar(conn, _sql(m, "done_sql", params))
        expected = db.scalar(conn, _sql(m, "expected_sql", params))
        pct = _pct(done, expected)

        # Daily rate + trend. A metric with trend_sql (volume-fill) gets its real
        # per-day curve straight from the source table's DAT_CREAT — instant
        # history. Completion metrics have no per-day source, so the rate is the
        # delta vs the previous snapshot and the trend is the % history.
        if m.get("trend_sql"):
            pairs = db.fetch_pairs(conn, _sql(m, "trend_sql", params))
            trend = [(str(a), b) for (a, b) in pairs]
            trend_kind = "rate"
            daily_rate = pairs[-1][1] if pairs else None
        else:
            prev = db.previous_done(conn, slug, source_db, m["table"], m["key"], dat)
            daily_rate = (done - prev) if (done is not None and prev is not None) else None
            trend = [(str(a), b) for (a, b) in db.pct_history(conn, slug, m["key"])]
            trend_kind = "pct"

        row = {
            "REPORT_SLUG": slug, "SOURCE_DB": source_db, "TABLE_NAME": m["table"],
            "METRIC_KEY": m["key"], "DONE_COUNT": done, "EXPECTED_COUNT": expected,
            "PCT": pct, "DAILY_RATE": daily_rate,
            "DESCRIPTION": m["description"], "LONG_DESC": m.get("long_desc"),
            "DELETED": 0, "DISPLAY_ORDER": order,
            "ID_CREATOR": 0, "DAT_CREAT": dat, "ID_OWNER": 0,
            "TIM_UPDATED": tim, "ID_USER_UPDATED": 0,
        }
        db.upsert_snapshot(conn, row)
        # snapshot just written → reflect it in the % history trend
        if trend_kind == "pct" and (not trend or trend[-1][0] != str(dat)):
            trend.append((str(dat), pct))

        results.append({
            "key": m["key"], "description": m["description"],
            "long_desc": m.get("long_desc"), "warn_below": m.get("warn_below", 50),
            "done": done, "expected": expected, "pct": pct, "daily_rate": daily_rate,
            "trend": trend, "trend_kind": trend_kind,
            "rate_label": m.get("rate_label"),
        })
    return results


def write_artifacts(report, results, run_dt, db_label):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = run_dt.strftime("%Y%m%d")
    generated_at = run_dt.strftime("%Y-%m-%d %H:%M %Z")
    html_doc = render.render_report(report, results, generated_at, db_label)
    fname = f"{report['slug']}-{stamp}.html"
    path = os.path.join(OUTPUT_DIR, fname)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_doc)
    return fname


def write_index(report_files, run_dt):
    """A tiny landing page linking the day's reports, plus a stable latest copy."""
    stamp = run_dt.strftime("%Y%m%d")
    generated_at = run_dt.strftime("%Y-%m-%d %H:%M %Z")
    links = "\n".join(
        f'<li><a href="{f}">{f}</a></li>' for f in sorted(report_files)
    )
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>data-monitoring — {generated_at}</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:28px;color:#263238}}
h1{{font-size:18px}} li{{margin:4px 0}} .meta{{color:#90a4ae;font-size:12px}}</style>
</head><body>
<h1>data-monitoring reports</h1>
<div class="meta">Generated {generated_at}</div>
<ul>
{links}
</ul></body></html>
"""
    for name in (f"index-{stamp}.html", "index-latest.html"):
        with open(os.path.join(OUTPUT_DIR, name), "w", encoding="utf-8") as fh:
            fh.write(doc)


def prune(run_dt):
    """Delete *.html older than RETENTION_DAYS (NAS sync archives them first)."""
    if RETENTION_DAYS <= 0:
        return
    cutoff = run_dt.timestamp() - RETENTION_DAYS * 86400
    for path in glob.glob(os.path.join(OUTPUT_DIR, "*.html")):
        if os.path.basename(path).startswith("index-latest"):
            continue
        if os.path.getmtime(path) < cutoff:
            try:
                os.remove(path)
                print(f"pruned {os.path.basename(path)}")
            except OSError as exc:
                print(f"prune failed for {path}: {exc}", file=sys.stderr)


# --- sample mode: render the UI from seeded numbers, no DB, no writes -------

def _write_sample(slug, report, results, generated_at, filename):
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
    os.makedirs(out_dir, exist_ok=True)
    doc = render.render_report(report, results, generated_at, "sample / no DB")
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print(f"wrote {path}")


def _sample_wikipedia_refresh():
    """Seeded preview of the wikipedia-sections-refresh report.

    Mirrors the real manifest's metric keys and wording so the rendering of a
    just-started campaign (low percentages, short trend) can be checked offline.
    """
    report = {
        "slug": "wikipedia-sections-refresh",
        "title": "Wikipedia — refresh coverage since the fine-section split",
        "description": ("Freshness of the stored Wikipedia content after the H2+H3 fine-section "
                        "split went live on 2026-07-20 23:00 (Paris). SAMPLE DATA — numbers are "
                        "illustrative."),
    }
    base = datetime.date(2026, 7, 21)
    ndays = 3
    ent_rate = [(str(base + datetime.timedelta(days=i)), 11800 + i * 900) for i in range(ndays)]
    page_rate = [(str(base + datetime.timedelta(days=i)), 21400 + i * 1600) for i in range(ndays)]
    pct_hist = [(str(base + datetime.timedelta(days=i)), round(2.1 + i * 2.0, 2)) for i in range(ndays)]
    results = [
        {"key": "wikipedia_entity_refresh", "description": "Wikidata IDs re-crawled since the switch",
         "long_desc": "Distinct ID_WIKIDATA with a language row crawled at or after 2026-07-20 23:00, "
                      "over every ID the crawler has resolved a page for.",
         "warn_below": 50, "rate_label": "Wikidata IDs/day",
         "done": 37900, "expected": 566300, "pct": 6.69,
         "daily_rate": ent_rate[-1][1], "trend": ent_rate, "trend_kind": "rate"},
        {"key": "wikipedia_entity_refresh_all_langs", "description": "Wikidata IDs fully refreshed (every language row)",
         "long_desc": "Stricter variant: every language row of the entity crawled at or after the switch.",
         "warn_below": 50, "rate_label": "Wikidata IDs/day",
         "done": 35100, "expected": 566300, "pct": 6.20,
         "daily_rate": 11200, "trend": pct_hist, "trend_kind": "pct"},
        {"key": "wikipedia_page_lang_refresh", "description": "Page rows (Wikidata ID x language) re-crawled",
         "long_desc": "Row-level view: (ID_WIKIDATA, LANG) pairs crawled at or after the switch. The unit "
                      "the crawler actually processes, so the best basis for the ETA.",
         "warn_below": 50, "rate_label": "pages/day",
         "done": 69300, "expected": 1132600, "pct": 6.12,
         "daily_rate": page_rate[-1][1], "trend": page_rate, "trend_kind": "rate"},
        {"key": "wikipedia_entity_refresh_success", "description": "Wikidata IDs re-crawled AND parsed successfully",
         "long_desc": "LAST_SUCCESS_AT at or after the switch. A widening gap with the headline metric "
                      "means the re-crawl runs but fails, and no new sections are written.",
         "warn_below": 50, "rate_label": "Wikidata IDs/day",
         "done": 36800, "expected": 566300, "pct": 6.50,
         "daily_rate": 11500, "trend": pct_hist, "trend_kind": "pct"},
        {"key": "wikipedia_section_entity_refresh", "description": "Wikidata IDs whose stored sections were rewritten",
         "long_desc": "The content metric: entities with at least one section row rewritten since the "
                      "switch — those rows carry the H2+H3 fine split.",
         "warn_below": 50, "rate_label": "Wikidata IDs/day",
         "done": 34200, "expected": 548900, "pct": 6.23,
         "daily_rate": 10900, "trend": pct_hist, "trend_kind": "pct"},
        {"key": "wikipedia_fine_section_split_share", "description": "Share of rewritten sections that are H3 sub-sections",
         "long_desc": "Signature check, not a completion target: share of the sections written since the "
                      "switch whose TITLE has the composite 'Parent - Child' shape.",
         "warn_below": 5, "rate_label": "sections/day",
         "done": 402700, "expected": 1284100, "pct": 31.36,
         "daily_rate": 128000, "trend": [(d, round(30.5 + i * 0.4, 2)) for i, (d, _) in enumerate(pct_hist)],
         "trend_kind": "pct"},
    ]
    _write_sample("wikipedia-sections-refresh", report, results,
                  "2026-07-23 06:30 (SAMPLE)", "wikipedia-sections-refresh-20260723.html")


def _sample_tmdb_neighbours():
    """Seeded preview of the tmdb-neighbours-backfill report.

    Mirrors the manifest's metric keys and wording so the two-angle layout
    (re-crawl progress vs table fill) can be checked offline.
    """
    report = {
        "slug": "tmdb-neighbours-backfill",
        "title": "TMDb — similar & recommendations backfill",
        "description": ("Progress of the TMDb similar / recommendations backfill (TMDB-CRAWLER-022/023), "
                        "live since 2026-07-07 ~16:00 (Paris). SAMPLE DATA — numbers are illustrative."),
    }
    base = datetime.date(2026, 7, 7)
    ndays = 17
    mv_rate = [(str(base + datetime.timedelta(days=i)), 26000 + (i % 4) * 3000 + i * 400) for i in range(ndays)]
    se_rate = [(str(base + datetime.timedelta(days=i)), 3200 + (i % 3) * 300 + i * 40) for i in range(ndays)]
    row_rate = [(str(base + datetime.timedelta(days=i)), 210000 + (i % 5) * 12000) for i in range(ndays)]
    row_rate_se = [(str(base + datetime.timedelta(days=i)), 41000 + (i % 5) * 2600) for i in range(ndays)]
    results = [
        {"key": "movie_recrawl_progress", "description": "Movies re-crawled since the feature shipped",
         "long_desc": "Non-deleted movies whose TIM_UPDATED is at or after 2026-07-07 16:00, over all "
                      "non-deleted movies. The true completion signal; the sparkline is the best ETA basis.",
         "warn_below": 50, "rate_label": "movies/day",
         "done": 486300, "expected": 912400, "pct": 53.30,
         "daily_rate": mv_rate[-1][1], "trend": mv_rate, "trend_kind": "rate"},
        {"key": "serie_recrawl_progress", "description": "Series re-crawled since the feature shipped",
         "long_desc": "Series mirror; far fewer than movies, so this half typically finishes first.",
         "warn_below": 50, "rate_label": "series/day",
         "done": 138200, "expected": 204100, "pct": 67.71,
         "daily_rate": se_rate[-1][1], "trend": se_rate, "trend_kind": "rate"},
        {"key": "movie_similar_fill", "description": "Movies with a stored 'similar' set",
         "long_desc": "Distinct ID_MOVIE in T_WC_TMDB_MOVIE_SIMILAR over all non-deleted movies. Cannot "
                      "reach 100%: TMDb returns no similar set for many obscure titles.",
         "warn_below": 30, "rate_label": "rows/day",
         "done": 372800, "expected": 912400, "pct": 40.86,
         "daily_rate": row_rate[-1][1], "trend": row_rate, "trend_kind": "rate"},
        {"key": "movie_recommendation_fill", "description": "Movies with a stored 'recommendations' set",
         "long_desc": "Distinct ID_MOVIE in T_WC_TMDB_MOVIE_RECOMMENDATION. Sparser than similar, so it "
                      "sits below movie_similar_fill for the same crawl progress.",
         "warn_below": 30, "rate_label": "rows/day",
         "done": 331500, "expected": 912400, "pct": 36.33,
         "daily_rate": row_rate[-1][1] - 40000, "trend": [(d, v - 40000) for d, v in row_rate], "trend_kind": "rate"},
        {"key": "serie_similar_fill", "description": "Series with a stored 'similar' set",
         "long_desc": "Distinct ID_SERIE in T_WC_TMDB_SERIE_SIMILAR; series mirror of movie_similar_fill.",
         "warn_below": 30, "rate_label": "rows/day",
         "done": 96400, "expected": 204100, "pct": 47.23,
         "daily_rate": row_rate_se[-1][1], "trend": row_rate_se, "trend_kind": "rate"},
        {"key": "serie_recommendation_fill", "description": "Series with a stored 'recommendations' set",
         "long_desc": "Distinct ID_SERIE in T_WC_TMDB_SERIE_RECOMMENDATION; series mirror of "
                      "movie_recommendation_fill.",
         "warn_below": 30, "rate_label": "rows/day",
         "done": 88900, "expected": 204100, "pct": 43.56,
         "daily_rate": row_rate_se[-1][1] - 6000, "trend": [(d, v - 6000) for d, v in row_rate_se], "trend_kind": "rate"},
    ]
    _write_sample("tmdb-neighbours-backfill", report, results,
                  "2026-07-24 06:30 (SAMPLE)", "tmdb-neighbours-backfill-20260724.html")


def _sample():
    _sample_tmdb_tv()
    _sample_wikipedia_refresh()
    _sample_tmdb_neighbours()


def _sample_tmdb_tv():
    run_dt = datetime.datetime(2026, 6, 23, 6, 30)
    report = {
        "slug": "tmdb-tv-coverage",
        "title": "TMDb — TV season & episode coverage",
        "description": ("Progress of the long-running TV season/episode backfill driven by the "
                        "tmdb-crawler series-refresh processes (4 / 28 / 33 + tv/changes). "
                        "SAMPLE DATA — numbers are illustrative."),
    }
    # a ~7-week daily episode-gather curve (deterministic, illustrative)
    base = datetime.date(2026, 5, 5)
    ep_rate = [(str(base + datetime.timedelta(days=i)),
                9000 + (i % 7) * 1200 + (i * 130)) for i in range(49)]
    se_rate = [(str(base + datetime.timedelta(days=i)),
                700 + (i % 5) * 90 + (i * 8)) for i in range(49)]
    pct_hist = [(str(datetime.date(2026, 6, 23) - datetime.timedelta(days=d)),
                 round(61.8 - d * 0.35, 2)) for d in range(20, -1, -1)]
    pct_hist_se = [(str(datetime.date(2026, 6, 23) - datetime.timedelta(days=d)),
                    round(74.2 - d * 0.22, 2)) for d in range(20, -1, -1)]
    results = [
        {"key": "episode_series_completion", "description": "Series with episodes fully gathered",
         "long_desc": "Series whose TIM_EPISODES_COMPLETED is stamped by the crawler, over all "
                      "non-deleted series. Resets when a series is refreshed (TIM_UPDATED > 30d).",
         "warn_below": 60, "done": 42180, "expected": 68310, "pct": 61.75,
         "daily_rate": 240, "trend": pct_hist, "trend_kind": "pct"},
        {"key": "episode_volume_fill", "description": "Episodes stored vs episodes TMDb reports",
         "long_desc": "COUNT(T_WC_TMDB_EPISODE) over SUM(NUMBER_OF_EPISODES). Denominator drifts "
                      "with series-row freshness; treat as an estimate.",
         "warn_below": 50, "done": 1254300, "expected": 1601200, "pct": 78.34,
         "daily_rate": ep_rate[-1][1], "trend": ep_rate, "trend_kind": "rate"},
        {"key": "season_series_completion", "description": "Series with seasons fully gathered",
         "long_desc": "Series whose TIM_SEASONS_COMPLETED is stamped, over all non-deleted series.",
         "warn_below": 60, "done": 50690, "expected": 68310, "pct": 74.21,
         "daily_rate": 310, "trend": pct_hist_se, "trend_kind": "pct"},
        {"key": "season_volume_fill", "description": "Seasons stored vs seasons TMDb reports",
         "long_desc": "COUNT(T_WC_TMDB_SEASON) over SUM(NUMBER_OF_SEASONS).",
         "warn_below": 50, "done": 198400, "expected": 232900, "pct": 85.19,
         "daily_rate": se_rate[-1][1], "trend": se_rate, "trend_kind": "rate"},
    ]
    _write_sample("tmdb-tv-coverage", report, results,
                  "2026-06-23 06:30 (SAMPLE)", "tmdb-tv-coverage-20260623.html")


def _find_manifests(report=None):
    manifests = sorted(glob.glob(os.path.join(REPORTS_DIR, "*.yaml")))
    if report:
        manifests = [p for p in manifests
                     if os.path.splitext(os.path.basename(p))[0] == report]
        if not manifests:
            sys.exit(f"no manifest for report '{report}' in {REPORTS_DIR}")
    return manifests


def _print_sql(report=None):
    """Dry run: parse the manifests and print every metric's resolved SQL.

    No DB needed — this is how a new manifest (placeholders substituted, YAML well
    formed, metric keys unique) is checked before it reaches the VPS.
    """
    seen = {}
    for path in _find_manifests(report):
        manifest = load_manifest(path)
        params = manifest.get("params") or {}
        print(f"\n=== {manifest['slug']} ({len(manifest['metrics'])} metrics, "
              f"source_db={manifest.get('source_db', SOURCE_DEFAULT)}) ===")
        for m in manifest["metrics"]:
            # METRIC_KEY is part of the snapshot unique key together with
            # (DAT_CREAT, SOURCE_DB, TABLE_NAME) — a duplicate would silently
            # overwrite another metric's row.
            dupkey = (manifest.get("source_db", SOURCE_DEFAULT), m["table"], m["key"])
            if dupkey in seen:
                print(f"  !! DUPLICATE metric key {m['key']} on {m['table']} "
                      f"(also in {seen[dupkey]}) — snapshot rows would collide")
            seen[dupkey] = manifest["slug"]
            print(f"\n  [{m['key']}] {m['description']}")
            for field in ("done_sql", "expected_sql", "trend_sql"):
                if m.get(field):
                    print(f"    {field}: {_sql(m, field, params)}")


def main():
    ap = argparse.ArgumentParser(description="data-monitoring report generator")
    ap.add_argument("--report", help="run a single report slug (default: all)")
    ap.add_argument("--sample", action="store_true", help="render a seeded sample, no DB")
    ap.add_argument("--print-sql", action="store_true",
                    help="print each metric's resolved SQL and exit, no DB")
    args = ap.parse_args()

    if args.sample:
        _sample()
        return

    if args.print_sql:
        _print_sql(args.report)
        return

    import db
    run_dt = _now()
    manifests = _find_manifests(args.report)

    # Fail loudly rather than emit an empty index — a missing reports/ dir in the
    # container is a deploy error, not a no-op.
    print(f"reports dir: {REPORTS_DIR} — {len(manifests)} manifest(s) found")
    if not manifests:
        sys.exit(f"ERROR: no report manifests in {REPORTS_DIR} "
                 "— was the reports/ directory deployed into the image?")

    db_label = f"{os.environ.get('DB_NAME', '?')}@{os.environ.get('DB_HOST', '?')}"
    conn = db.get_connection()
    written = []
    try:
        for path in manifests:
            manifest = load_manifest(path)
            results = run_report(conn, manifest, run_dt)
            written.append(write_artifacts(manifest, results, run_dt, db_label))
            print(f"report '{manifest['slug']}': {len(results)} metrics")
            for r in results:
                print(f"  {r['key']}: {r['done']}/{r['expected']} = {r['pct']}%")
    finally:
        conn.close()

    write_index(written, run_dt)
    prune(run_dt)


if __name__ == "__main__":
    main()
