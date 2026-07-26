#!/usr/bin/env python3
"""One-shot: inject the prev/next day-nav bar into ALREADY-generated report pages.

Past report pages cannot be faithfully regenerated (a pipeline card's step state
comes from server variables that are overwritten in place, and a sparkline "as of
that day" is not reconstructible), so instead of re-rendering them this tool does a
surgical edit: it inserts a self-contained, inline-styled <nav> block right after
</header> in each existing `<slug>-YYYYMMDD.html`.

Why a separate script (not render.py):
  - render._day_nav_bar relies on CSS classes defined in freshly rendered pages;
    old pages lack those rules, so the retrofit nav carries its own inline styles.
  - prev/next existence is read from the files ACTUALLY present in the directory,
    so an archive shows a link only to a day that really exists (more accurate than
    the forward-looking generation-time logic).

Idempotent: a page that already contains a day-nav is skipped, so it is safe to run
on both the live OUTPUT_DIR and the NAS archive, repeatedly.

Usage:
  python backfill_day_nav.py [DIR]         # inject into DIR (default: $OUTPUT_DIR or /shared)
  python backfill_day_nav.py [DIR] --dry-run
"""
import argparse
import datetime
import glob
import html
import os
import re

# `<slug>-YYYYMMDD.html`, slug may itself contain hyphens; the date is the last group.
_NAME_RE = re.compile(r"^(?P<slug>.+)-(?P<date>\d{8})\.html$")
_HEADER_CLOSE = "</header>"
_NAV_MARKER = 'class="day-nav"'

_NAV_WRAP = (
    '<nav class="day-nav" style="display:flex;align-items:center;'
    'justify-content:space-between;gap:12px;padding:10px 28px;background:#eceff1;'
    'border-bottom:1px solid #cfd8dc;flex-wrap:wrap;'
    'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
    '<div style="display:flex;align-items:center;gap:14px;font-size:13px;">{days}</div>'
    '<a href="index-latest.html" style="font-size:12px;color:#607d8b;'
    'text-decoration:none;">all reports</a></nav>'
)


def _link(href, text):
    return (f'<a href="{html.escape(href)}" style="color:#1976d2;text-decoration:none;'
            f'font-weight:600;font-variant-numeric:tabular-nums;">{html.escape(text)}</a>')


def _disabled(text):
    return (f'<span style="color:#b0bec5;font-variant-numeric:tabular-nums;" '
            f'title="not generated or pruned">{html.escape(text)}</span>')


def _nav_html(slug, day, present):
    """Build the nav block for one page. `present` is the set of (slug, date) on disk."""
    prev_d = day - datetime.timedelta(days=1)
    next_d = day + datetime.timedelta(days=1)
    prev_stamp, next_stamp = prev_d.strftime("%Y%m%d"), next_d.strftime("%Y%m%d")
    prev = (_link(f"{slug}-{prev_stamp}.html", "◀ " + prev_d.isoformat())
            if (slug, prev_stamp) in present else _disabled("◀ " + prev_d.isoformat()))
    today = (f'<span style="color:#263238;font-weight:700;'
             f'font-variant-numeric:tabular-nums;">{day.isoformat()}</span>')
    nxt = (_link(f"{slug}-{next_stamp}.html", next_d.isoformat() + " ▶")
           if (slug, next_stamp) in present else _disabled(next_d.isoformat() + " ▶"))
    return _NAV_WRAP.format(days=prev + today + nxt)


def backfill(directory, dry_run=False):
    files = sorted(glob.glob(os.path.join(directory, "*.html")))
    present = set()
    parsed = []
    for path in files:
        m = _NAME_RE.match(os.path.basename(path))
        if not m or m.group("slug") in ("index", "index-latest"):
            continue
        try:
            day = datetime.datetime.strptime(m.group("date"), "%Y%m%d").date()
        except ValueError:
            continue
        present.add((m.group("slug"), m.group("date")))
        parsed.append((path, m.group("slug"), m.group("date"), day))

    injected = skipped = 0
    for path, slug, _stamp, day in parsed:
        text = open(path, encoding="utf-8").read()
        if _NAV_MARKER in text:
            skipped += 1
            continue
        idx = text.find(_HEADER_CLOSE)
        if idx == -1:
            print(f"  ! no </header> in {os.path.basename(path)} - skipped")
            skipped += 1
            continue
        nav = _nav_html(slug, day, present)
        cut = idx + len(_HEADER_CLOSE)
        new_text = text[:cut] + "\n" + nav + text[cut:]
        if dry_run:
            print(f"  would inject nav -> {os.path.basename(path)}")
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
            print(f"  injected nav -> {os.path.basename(path)}")
        injected += 1
    verb = "would inject" if dry_run else "injected"
    print(f"{verb} {injected} page(s); skipped {skipped} (already had nav / no header).")
    return injected


def main():
    ap = argparse.ArgumentParser(description="Retrofit the day-nav bar into old report pages")
    ap.add_argument("directory", nargs="?",
                    default=os.environ.get("OUTPUT_DIR", "/shared"),
                    help="directory of generated report HTML (default: $OUTPUT_DIR or /shared)")
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = ap.parse_args()
    print(f"backfill day-nav in: {args.directory}")
    backfill(args.directory, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
