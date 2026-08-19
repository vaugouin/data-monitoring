#!/usr/bin/env python3
"""data-monitoring - nightly coverage/progress reports for long-running backfills.

For each report manifest in reports/<slug>.yaml it:
  1. runs read-only SQL against the monitored database,
  2. writes one snapshot row per metric per day to T_WC_DATA_MONITORING_SNAPSHOT
     (idempotent upsert), computing the daily rate from prior snapshots,
  3. renders a self-contained HTML artifact <slug>-YYYYMMDD.html into the
     shared_data output directory, plus a daily index and index-latest.html,
  4. prunes HTML artifacts older than RETENTION_DAYS (history stays in the table;
     the NAS sync archives the files before they expire).

Source-agnostic by design: a new campaign - TMDb, Wikidata, anything - is a new
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

    A manifest that declares `params:` may write `{name}` placeholders in its SQL -
    used to keep a value repeated across many metrics (a campaign cutoff date, say)
    in one place. Without `params:` the SQL is passed through untouched, so existing
    manifests keep working even if they contain braces.
    """
    sql = metric.get(key)
    if sql and params:
        sql = sql.format(**params)
    return sql


def _parse_dt(s):
    """Parse a 'YYYY-MM-DD HH:MM:SS' server-variable timestamp; None on failure."""
    if not s:
        return None
    try:
        return datetime.datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _fmt_duration(seconds):
    if seconds is None or seconds < 0:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _build_pipeline(vars_, steps, run_dt, overall_status):
    """Turn <prefix>step<code>{status,startedat,finishedat} vars into step states.

    Returns (steplist, done_count). Each step: code, label, state
    (done|running|failed|pending), started, finished, duration. A step is FAILED
    when the whole run is in FAILURE and that step is the one left RUNNING.
    """
    now = run_dt.replace(tzinfo=None)
    out = []
    done = 0
    for st in steps:
        code = st["code"]
        status = (vars_.get(f"step{code}status", (None,))[0] or "").upper()
        started = _parse_dt(vars_.get(f"step{code}startedat", (None,))[0])
        finished = _parse_dt(vars_.get(f"step{code}finishedat", (None,))[0])
        if status == "SUCCESS":
            state = "done"
            done += 1
        elif status == "RUNNING":
            state = "failed" if overall_status == "FAILURE" else "running"
        else:
            state = "pending"
        if finished and started:
            duration = _fmt_duration((finished - started).total_seconds())
        elif state == "running" and started:
            duration = _fmt_duration((now - started).total_seconds())
        else:
            duration = None
        out.append({
            "code": code, "label": st["label"], "state": state,
            "started": str(started) if started else None,
            "finished": str(finished) if finished else None,
            "duration": duration,
        })
    return out, done


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
        kind = m.get("kind", "coverage")

        # A `pipeline` metric tracks a multi-step batch job whose orchestrator
        # writes one T_WC_SERVER_VARIABLE row per step (<prefix>step<code>status /
        # startedat / finishedat). It renders as a step timeline; the snapshot
        # stores steps-done / total so the daily completion % builds a trend.
        if kind == "pipeline":
            prefix = m["var_prefix"]
            allvars = db.server_variables(conn, prefix)
            # strip the prefix so keys are 'step101status', 'status', ...
            vars_ = {k[len(prefix):]: v for k, v in allvars.items()}
            overall_status = (vars_.get("status", (None,))[0] or "").upper()
            steps = m["steps"]
            steplist, done = _build_pipeline(vars_, steps, run_dt, overall_status)
            total = len(steps)
            pct = _pct(done, total)
            row = {
                "REPORT_SLUG": slug, "SOURCE_DB": source_db, "TABLE_NAME": m["table"],
                "METRIC_KEY": m["key"], "DONE_COUNT": done, "EXPECTED_COUNT": total,
                "PCT": pct, "DAILY_RATE": None,
                "DESCRIPTION": m["description"], "LONG_DESC": m.get("long_desc"),
                "DELETED": 0, "DISPLAY_ORDER": order,
                "ID_CREATOR": 0, "DAT_CREAT": dat, "ID_OWNER": 0,
                "TIM_UPDATED": tim, "ID_USER_UPDATED": 0,
            }
            db.upsert_snapshot(conn, row)
            trend = [(str(a), b) for (a, b) in db.pct_history(conn, slug, m["key"])]
            if not trend or trend[-1][0] != str(dat):
                trend.append((str(dat), pct))
            results.append({
                "key": m["key"], "description": m["description"],
                "long_desc": m.get("long_desc"), "kind": "pipeline",
                "done": done, "expected": total, "pct": pct,
                "trend": trend, "trend_kind": "pct",
                "steps": steplist, "overall_status": overall_status or "UNKNOWN",
                "current_process": vars_.get("currentprocess", (None,))[0],
                "started_at": vars_.get("startdatetime", (None,))[0],
                "ended_at": vars_.get("enddatetime", (None,))[0],
                "runtime": vars_.get("totalruntime", (None,))[0],
                "last_error": vars_.get("lasterror", (None,))[0] if overall_status == "FAILURE" else None,
                "alert": overall_status == "FAILURE",
            })
            continue

        # An `alert_zero` metric is an INVARIANT, not a coverage %: its done_sql
        # counts rows that must never exist (a regression guard). done == 0 is
        # healthy; done > 0 raises a page-level alert. It has no denominator and no
        # percentage; its trend is the raw count over the snapshot history, which
        # should stay flat on zero.
        if kind == "alert_zero":
            done = db.scalar(conn, _sql(m, "done_sql", params)) or 0
            expected = 0                     # the target: zero offending rows
            pct = None
            daily_rate = None
            trend = [(str(a), b) for (a, b) in db.done_history(conn, slug, m["key"])]
            trend_kind = "count"
        else:
            done = db.scalar(conn, _sql(m, "done_sql", params))
            expected = db.scalar(conn, _sql(m, "expected_sql", params))
            pct = _pct(done, expected)

            # Daily rate + trend. A metric with trend_sql (volume-fill) gets its real
            # per-day curve straight from the source table's DAT_CREAT - instant
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
        # snapshot just written → reflect today's point in the accumulated trend
        if trend_kind == "pct" and (not trend or trend[-1][0] != str(dat)):
            trend.append((str(dat), pct))
        elif trend_kind == "count" and (not trend or trend[-1][0] != str(dat)):
            trend.append((str(dat), done))

        results.append({
            "key": m["key"], "description": m["description"],
            "long_desc": m.get("long_desc"), "warn_below": m.get("warn_below", 50),
            "done": done, "expected": expected, "pct": pct, "daily_rate": daily_rate,
            "trend": trend, "trend_kind": trend_kind,
            "rate_label": m.get("rate_label"), "show_eta": m.get("show_eta", True),
            "kind": kind,
            "alert": (kind == "alert_zero" and (done or 0) > 0),
        })
    return results


def _day_nav(slug, run_dt, out_dir):
    """Prev/next-day links for the same report, so you can walk it over time.

    Filenames are `<slug>-YYYYMMDD.html`, so the neighbours are pure date
    arithmetic. The PREVIOUS day's file either exists on disk (link it) or was
    never generated / has been pruned (show it disabled). The NEXT day's file
    cannot exist yet on the latest run, but tomorrow's run creates it and its own
    page links back here — so the forward link is always rendered by date.
    """
    d = run_dt.date()
    prev_d = d - datetime.timedelta(days=1)
    next_d = d + datetime.timedelta(days=1)
    prev_name = f"{slug}-{prev_d.strftime('%Y%m%d')}.html"
    next_name = f"{slug}-{next_d.strftime('%Y%m%d')}.html"
    return {
        "prev_href": prev_name,
        "prev_label": prev_d.isoformat(),
        "prev_exists": os.path.exists(os.path.join(out_dir, prev_name)),
        "today_label": d.isoformat(),
        "next_href": next_name,
        "next_label": next_d.isoformat(),
        "index_href": "index-latest.html",
    }


def write_artifacts(report, results, run_dt, db_label):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = run_dt.strftime("%Y%m%d")
    generated_at = run_dt.strftime("%Y-%m-%d %H:%M %Z")
    nav = _day_nav(report["slug"], run_dt, OUTPUT_DIR)
    html_doc = render.render_report(report, results, generated_at, db_label, nav=nav)
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
<title>data-monitoring - {generated_at}</title>
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
    # Illustrative day-nav (prev shown active, next by date) so the sample shows the
    # over-time navigation bar; the real hrefs are computed per run in write_artifacts.
    day = filename.rsplit("-", 1)[-1].replace(".html", "")
    try:
        d = datetime.datetime.strptime(day, "%Y%m%d").date()
        nav = {
            "prev_href": f"{slug}-{(d - datetime.timedelta(days=1)).strftime('%Y%m%d')}.html",
            "prev_label": (d - datetime.timedelta(days=1)).isoformat(), "prev_exists": True,
            "today_label": d.isoformat(),
            "next_href": f"{slug}-{(d + datetime.timedelta(days=1)).strftime('%Y%m%d')}.html",
            "next_label": (d + datetime.timedelta(days=1)).isoformat(),
            "index_href": "index-latest.html",
        }
    except ValueError:
        nav = None
    doc = render.render_report(report, results, generated_at, "sample / no DB", nav=nav)
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
        "title": "Wikipedia - refresh coverage since the fine-section split",
        "description": ("Freshness of the stored Wikipedia content after the H2+H3 fine-section "
                        "split went live on 2026-07-20 23:00 (Paris). SAMPLE DATA - numbers are "
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
                      "switch - those rows carry the H2+H3 fine split.",
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
        "title": "TMDb - similar & recommendations backfill",
        "description": ("Progress of the TMDb similar / recommendations backfill (TMDB-CRAWLER-022/023), "
                        "live since 2026-07-07 ~16:00 (Paris). SAMPLE DATA - numbers are illustrative."),
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


def _sample_tmdb_company_wikidata():
    """Seeded preview of the tmdb-company-wikidata report.

    Mirrors the manifest's metric keys and wording so the all-upward framing
    (coverage + match quality) can be checked offline.
    """
    report = {
        "slug": "tmdb-company-wikidata",
        "title": "TMDb - company Wikidata linking (Process 63)",
        "description": ("Progress and quality of the TMDb company -> Wikidata backfill (Process 63, "
                        "TMDB-MOVIE-PREPROCESS-015). SAMPLE DATA - numbers are illustrative."),
    }
    base = datetime.date(2026, 6, 25)
    ndays = 30
    search_rate = [(str(base + datetime.timedelta(days=i)), 3000 if i < 28 else 1800) for i in range(ndays)]
    pct_cov = [(str(base + datetime.timedelta(days=i)), round(min(100.0, 12.0 + i * 3.0), 2)) for i in range(ndays)]
    pct_usable = [(str(base + datetime.timedelta(days=i)), round(min(41.0, 6.0 + i * 1.2), 2)) for i in range(ndays)]
    pct_match = [(str(base + datetime.timedelta(days=i)), round(min(58.0, 44.0 + i * 0.5), 2)) for i in range(ndays)]
    pct_clean = [(str(base + datetime.timedelta(days=i)), round(min(62.0, 55.0 + i * 0.25), 2)) for i in range(ndays)]
    results = [
        {"key": "company_wikidata_attempted", "description": "Companies reached by the backfill",
         "long_desc": "Eligible companies whose TIM_WIKIPEDIA_SEARCH is set, over all eligible companies. "
                      "The frontier-coverage metric; sparkline = companies searched per day.",
         "warn_below": 50, "rate_label": "companies/day",
         "done": 78400, "expected": 86200, "pct": 90.95,
         "daily_rate": search_rate[-1][1], "trend": search_rate, "trend_kind": "rate"},
        {"key": "company_wikidata_usable", "description": "Companies with a usable Wikidata link",
         "long_desc": "Eligible companies with a Wikidata id at CONFIDENCE >= 0.9, over all eligible "
                      "companies. Cannot reach 100%: many small companies have no Wikidata entity.",
         "warn_below": 20, "done": 34600, "expected": 86200, "pct": 40.14,
         "daily_rate": None, "trend": pct_usable, "trend_kind": "pct"},
        {"key": "company_wikidata_linked", "description": "Companies linked (usable + quarantine)",
         "long_desc": "Eligible companies carrying any Wikidata id, quarantine included. The gap with the "
                      "usable metric is the quarantine backlog.",
         "warn_below": 25, "done": 55300, "expected": 86200, "pct": 64.15,
         "daily_rate": None, "trend": pct_cov, "trend_kind": "pct"},
        {"key": "company_wikidata_match_rate", "description": "Match rate among searched companies",
         "long_desc": "Of the companies searched, the share that got a link. The direct read on the "
                      "resolution bottleneck (acronyms, variants, special chars).",
         "warn_below": 40, "done": 55300, "expected": 78400, "pct": 70.54,
         "daily_rate": None, "trend": pct_match, "trend_kind": "pct"},
        {"key": "company_wikidata_clean_link_share", "description": "Clean links (share not quarantined)",
         "long_desc": "Of the linked companies, the share at usable confidence rather than quarantined at "
                      "0.50. 100% = nothing waiting for review.",
         "warn_below": 60, "done": 34600, "expected": 55300, "pct": 62.57,
         "daily_rate": None, "trend": pct_clean, "trend_kind": "pct"},
    ]
    _write_sample("tmdb-company-wikidata", report, results,
                  "2026-07-25 06:30 (SAMPLE)", "tmdb-company-wikidata-20260725.html")


def _sample_tmdb_poster_invariants():
    """Seeded preview of the tmdb-poster-invariants report.

    Deliberately shows ONE breached invariant (movies) so the alert banner + red
    card render alongside the healthy (0) card. Real runs are expected all-zero.
    """
    report = {
        "slug": "tmdb-poster-invariants",
        "title": "TMDb - poster invariants (FR posters must not be clamped at 0)",
        "description": ("Regression guard: no French poster may sit at DISPLAY_ORDER 0. Both counts must "
                        "always be 0. SAMPLE DATA - the movie breach below is illustrative."),
    }
    base = datetime.date(2026, 7, 19)
    flat_zero = [(str(base + datetime.timedelta(days=i)), 0) for i in range(7)]
    movie_spike = [(str(base + datetime.timedelta(days=i)), 0 if i < 6 else 3) for i in range(7)]
    results = [
        {"key": "fr_poster_at_zero_movie", "description": "FR posters clamped at DISPLAY_ORDER 0 (movies)",
         "long_desc": "Count of French movie poster rows at the reserved position 0. Must be 0; a breach "
                      "means a writer re-clamped a localized poster at 0.",
         "kind": "alert_zero", "done": 3, "expected": 0, "pct": None, "daily_rate": None,
         "alert": True, "trend": movie_spike, "trend_kind": "count"},
        {"key": "fr_poster_at_zero_serie", "description": "FR posters clamped at DISPLAY_ORDER 0 (series)",
         "long_desc": "Series equivalent: French poster rows at position 0. Must be 0.",
         "kind": "alert_zero", "done": 0, "expected": 0, "pct": None, "daily_rate": None,
         "alert": False, "trend": flat_zero, "trend_kind": "count"},
    ]
    _write_sample("tmdb-poster-invariants", report, results,
                  "2026-07-25 06:30 (SAMPLE)", "tmdb-poster-invariants-20260725.html")


def _sample_wikidata_pipeline():
    """Seeded preview of the wikidata-etl-pipeline report.

    A mid-run snapshot: download + pass1 done, pass2 running, the rest pending - so
    the timeline shows all four step states at once. Overall RUNNING.
    """
    report = {
        "slug": "wikidata-etl-pipeline",
        "title": "Wikidata - dump ETL pipeline (14 steps)",
        "description": ("Step-by-step progress of the multi-day Wikidata dump ingestion. SAMPLE DATA - "
                        "a mid-run snapshot (pass 2 in progress)."),
    }
    labels = [
        "Resolve / download the .bz2 dump", "Pass 1 - classification graph + core entity IDs",
        "Validate pass 1 output", "Pass 2 - entity rows + statements", "Validate pass 2 output",
        "Item-cache pass - referenced items", "Validate item-cache output",
        "Load NDJSON into staging tables", "Validate staging data",
        "Bulk-load target T_WC_WIKIDATA_* tables", "Validate target tables",
        "Resolve media resources", "Validate media resources", "Cleanup old import batches",
    ]
    # states for the 14 steps: 101 download done, 102-103 pass1 done, 104 running, rest pending
    states = ["done", "done", "done", "running"] + ["pending"] * 10
    times = {
        101: ("2026-07-26 13:02", "2026-07-26 18:41", "5h39m"),
        102: ("2026-07-26 18:41", "2026-07-26 23:07", "4h26m"),
        103: ("2026-07-26 23:07", "2026-07-26 23:09", "2m"),
        104: ("2026-07-26 23:09", None, "7h30m"),
    }
    steps = []
    for i, (label, state) in enumerate(zip(labels, states), start=0):
        code = 101 + i
        started, finished, dur = times.get(code, (None, None, None))
        steps.append({"code": code, "label": label, "state": state,
                      "started": started, "finished": finished, "duration": dur})
    done = sum(1 for s in steps if s["state"] == "done")
    base = datetime.date(2026, 7, 26)
    trend = [(str(base), round(100.0 * done / 14, 2))]
    results = [{
        "key": "wikidata_etl_pipeline", "description": "Wikidata dump ETL - 14-step pipeline",
        "long_desc": "Live step timeline read from the crawler's server variables; the bar and daily "
                     "trend are steps-completed / 14.",
        "kind": "pipeline", "done": done, "expected": 14, "pct": round(100.0 * done / 14, 2),
        "trend": trend, "trend_kind": "pct", "steps": steps, "overall_status": "RUNNING",
        "current_process": "104: run ETL pass2", "started_at": "2026-07-26 13:02:11",
        "ended_at": None, "runtime": "RUNNING", "last_error": None, "alert": False,
    }]
    _write_sample("wikidata-etl-pipeline", report, results,
                  "2026-07-27 06:30 (SAMPLE)", "wikidata-etl-pipeline-20260727.html")


def _sample_tmdb_release_dates():
    """Seeded preview of additive movie release-date coverage and quality."""
    report = {
        "slug": "tmdb-release-dates-coverage",
        "title": "TMDb - movie release dates coverage",
        "description": ("Coverage, freshness and structural quality of additive TMDb movie "
                        "release-date snapshots. SAMPLE DATA - numbers are illustrative."),
    }
    base = datetime.date(2026, 8, 16)
    gather_rate = [(str(base + datetime.timedelta(days=i)), 5000) for i in range(4)]
    fresh_history = [(str(base + datetime.timedelta(days=i)), 100.0) for i in range(4)]
    fill_history = [(str(base + datetime.timedelta(days=i)), 89.9 + i * 0.5)
                    for i in range(4)]
    quality_history = [(str(base + datetime.timedelta(days=i)), 99.65 + i * 0.03)
                       for i in range(4)]
    results = [
        {"key": "movie_release_dates_completion",
         "description": "Eligible movies with release dates completed",
         "long_desc": "The parent completion marker is authoritative, including valid empty responses.",
         "warn_below": 50, "rate_label": "movies/day",
         "done": 124600, "expected": 905000, "pct": 13.77,
         "daily_rate": gather_rate[-1][1], "trend": gather_rate, "trend_kind": "rate"},
        {"key": "movie_release_dates_fresh_35d",
         "description": "Completed release-date snapshots refreshed within 35 days",
         "long_desc": "Rolling freshness of completed snapshots; no ETA because the share can decrease.",
         "warn_below": 80, "rate_label": "movies/day", "show_eta": False,
         "done": 124600, "expected": 124600, "pct": 100.0,
         "daily_rate": 5000, "trend": fresh_history, "trend_kind": "pct"},
        {"key": "movie_release_dates_fill",
         "description": "Completed movies with at least one release event",
         "long_desc": "A valid empty TMDb response writes no child row, so this is not completion.",
         "warn_below": 5, "rate_label": "movies/day", "show_eta": False,
         "done": 113900, "expected": 124600, "pct": 91.41,
         "daily_rate": 4510, "trend": fill_history, "trend_kind": "pct"},
        {"key": "movie_release_dates_parsed_share",
         "description": "Release events with a parsed timestamp",
         "long_desc": "Share of current rows whose raw source date parsed into TIM_RELEASE.",
         "warn_below": 99, "rate_label": "rows/day", "show_eta": False,
         "done": 1659400, "expected": 1663000, "pct": 99.78,
         "daily_rate": None, "trend": quality_history, "trend_kind": "pct"},
        {"key": "movie_release_dates_structural_integrity",
         "description": "Release events with valid core fields",
         "long_desc": "ISO country, raw date, release type, JSON and response ordering checks.",
         "warn_below": 99, "rate_label": "rows/day", "show_eta": False,
         "done": 1658100, "expected": 1663000, "pct": 99.71,
         "daily_rate": -120, "trend": quality_history, "trend_kind": "pct"},
    ]
    _write_sample("tmdb-release-dates-coverage", report, results,
                  "2026-08-19 06:30 (SAMPLE)",
                  "tmdb-release-dates-coverage-20260819.html")


def _sample_tmdb_watch_providers():
    """Seeded preview of movie and series watch-provider snapshots."""
    report = {
        "slug": "tmdb-watch-providers-coverage",
        "title": "TMDb - watch providers coverage",
        "description": ("Coverage, freshness and attribution integrity of country-specific TMDb / "
                        "JustWatch snapshots. SAMPLE DATA - numbers are illustrative."),
    }
    base = datetime.date(2026, 8, 16)
    movie_rate = [(str(base + datetime.timedelta(days=i)), 5000) for i in range(4)]
    serie_rate = [(str(base + datetime.timedelta(days=i)), 5000 if i < 3 else 2800)
                  for i in range(4)]
    fresh_history = [(str(base + datetime.timedelta(days=i)), 100.0) for i in range(4)]
    movie_fill_history = [(str(base + datetime.timedelta(days=i)), 61.1 + i * 0.5)
                          for i in range(4)]
    serie_fill_history = [(str(base + datetime.timedelta(days=i)), 70.1 + i * 0.7)
                          for i in range(4)]
    quality_history = [(str(base + datetime.timedelta(days=i)), 99.91 + i * 0.02)
                       for i in range(4)]
    results = [
        {"key": "movie_watch_providers_completion",
         "description": "Eligible movies with watch providers completed",
         "long_desc": "Process 35 completion marker, including valid empty responses.",
         "warn_below": 50, "rate_label": "movies/day",
         "done": 119500, "expected": 905000, "pct": 13.20,
         "daily_rate": movie_rate[-1][1], "trend": movie_rate, "trend_kind": "rate"},
        {"key": "serie_watch_providers_completion",
         "description": "Eligible series with watch providers completed",
         "long_desc": "Process 36 completion marker for series-level provider snapshots.",
         "warn_below": 50, "rate_label": "series/day",
         "done": 42800, "expected": 148000, "pct": 28.92,
         "daily_rate": serie_rate[-1][1], "trend": serie_rate, "trend_kind": "rate"},
        {"key": "movie_watch_providers_fresh_35d",
         "description": "Completed movie-provider snapshots refreshed within 35 days",
         "long_desc": "Rolling freshness of completed movie snapshots.",
         "warn_below": 80, "rate_label": "movies/day", "show_eta": False,
         "done": 119500, "expected": 119500, "pct": 100.0,
         "daily_rate": 5000, "trend": fresh_history, "trend_kind": "pct"},
        {"key": "serie_watch_providers_fresh_35d",
         "description": "Completed series-provider snapshots refreshed within 35 days",
         "long_desc": "Rolling freshness of completed series snapshots.",
         "warn_below": 80, "rate_label": "series/day", "show_eta": False,
         "done": 42800, "expected": 42800, "pct": 100.0,
         "daily_rate": 2800, "trend": fresh_history, "trend_kind": "pct"},
        {"key": "movie_watch_providers_fill",
         "description": "Completed movies with at least one listed provider",
         "long_desc": "Current provider presence, not crawl completion; empty responses are valid.",
         "warn_below": 5, "rate_label": "movies/day", "show_eta": False,
         "done": 74800, "expected": 119500, "pct": 62.59,
         "daily_rate": 2900, "trend": movie_fill_history, "trend_kind": "pct"},
        {"key": "serie_watch_providers_fill",
         "description": "Completed series with at least one listed provider",
         "long_desc": "Series-level current availability, distinct from network metadata.",
         "warn_below": 5, "rate_label": "series/day", "show_eta": False,
         "done": 30900, "expected": 42800, "pct": 72.20,
         "daily_rate": -45, "trend": serie_fill_history, "trend_kind": "pct"},
        {"key": "movie_watch_provider_integrity",
         "description": "Movie-provider rows with valid core fields and attribution",
         "long_desc": "Country, mode, provider id, attribution link, timestamp and order checks.",
         "warn_below": 99, "rate_label": "rows/day", "show_eta": False,
         "done": 897740, "expected": 898000, "pct": 99.97,
         "daily_rate": None, "trend": quality_history, "trend_kind": "pct"},
        {"key": "serie_watch_provider_integrity",
         "description": "Series-provider rows with valid core fields and attribution",
         "long_desc": "Series counterpart of the movie provider integrity share.",
         "warn_below": 99, "rate_label": "rows/day", "show_eta": False,
         "done": 382910, "expected": 383000, "pct": 99.98,
         "daily_rate": None, "trend": quality_history, "trend_kind": "pct"},
    ]
    _write_sample("tmdb-watch-providers-coverage", report, results,
                  "2026-08-19 06:30 (SAMPLE)",
                  "tmdb-watch-providers-coverage-20260819.html")


def _sample():
    _sample_tmdb_tv()
    _sample_wikipedia_refresh()
    _sample_tmdb_neighbours()
    _sample_tmdb_company_wikidata()
    _sample_tmdb_poster_invariants()
    _sample_wikidata_pipeline()
    _sample_tmdb_release_dates()
    _sample_tmdb_watch_providers()


def _sample_tmdb_tv():
    run_dt = datetime.datetime(2026, 6, 23, 6, 30)
    report = {
        "slug": "tmdb-tv-coverage",
        "title": "TMDb - TV season & episode coverage",
        "description": ("Progress of the long-running TV season/episode backfill driven by the "
                        "tmdb-crawler series-refresh processes (4 / 28 / 33 + tv/changes). "
                        "SAMPLE DATA - numbers are illustrative."),
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

    No DB needed - this is how a new manifest (placeholders substituted, YAML well
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
            # (DAT_CREAT, SOURCE_DB, TABLE_NAME) - a duplicate would silently
            # overwrite another metric's row.
            dupkey = (manifest.get("source_db", SOURCE_DEFAULT), m["table"], m["key"])
            if dupkey in seen:
                print(f"  !! DUPLICATE metric key {m['key']} on {m['table']} "
                      f"(also in {seen[dupkey]}) - snapshot rows would collide")
            seen[dupkey] = manifest["slug"]
            print(f"\n  [{m['key']}] {m['description']}")
            if m.get("kind") == "pipeline":
                print(f"    pipeline: reads T_WC_SERVER_VARIABLE LIKE "
                      f"'{m['var_prefix']}%' - {len(m.get('steps') or [])} steps")
                continue
            if m.get("kind") == "alert_zero":
                print("    kind: alert_zero (count must stay 0)")
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

    # Fail loudly rather than emit an empty index - a missing reports/ dir in the
    # container is a deploy error, not a no-op.
    print(f"reports dir: {REPORTS_DIR} - {len(manifests)} manifest(s) found")
    if not manifests:
        sys.exit(f"ERROR: no report manifests in {REPORTS_DIR} "
                 "- was the reports/ directory deployed into the image?")

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
