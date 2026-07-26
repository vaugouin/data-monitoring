"""Self-contained HTML rendering for data-monitoring reports.

No external assets: all CSS is inlined and every chart is inline SVG, so a saved
report opens offline and archives cleanly to the NAS. Pure stdlib.
"""
import html


# ---- colour thresholds -----------------------------------------------------

def _bar_color(pct, warn_below):
    if pct is None:
        return "#9e9e9e"
    if pct < warn_below:
        return "#d9534f"      # red — under the warning floor
    if pct < 90:
        return "#f0ad4e"      # amber — getting there
    return "#5cb85c"          # green — effectively complete


def _fmt_int(n):
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", " ")   # thin space grouping


def _fmt_pct(p):
    return "—" if p is None else f"{p:.1f}%"


# ---- inline SVG ------------------------------------------------------------

def _coverage_bar(pct, warn_below):
    pct = pct or 0
    width = max(0.0, min(100.0, pct))
    color = _bar_color(pct, warn_below)
    return (
        '<svg class="bar" width="100%" height="22" preserveAspectRatio="none" '
        'viewBox="0 0 100 22">'
        '<rect x="0" y="0" width="100" height="22" rx="3" fill="#eceff1"/>'
        f'<rect x="0" y="0" width="{width:.2f}" height="22" rx="3" fill="{color}"/>'
        f'<line x1="{warn_below}" y1="0" x2="{warn_below}" y2="22" '
        'stroke="#607d8b" stroke-width="0.4" stroke-dasharray="1.5"/>'
        '</svg>'
    )


def _sparkline(points, kind):
    """points: list of (label, value). kind: 'rate' (per-day additions) or
    'pct' (completion % over snapshots)."""
    vals = [float(v) for (_, v) in points if v is not None]
    if len(vals) < 2:
        return '<span class="muted">trend builds up over time</span>'
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    w, h = 220.0, 40.0
    coords = []
    for i, v in enumerate(vals):
        x = (i / (n - 1)) * w
        y = h - ((v - lo) / span) * h
        coords.append(f"{x:.1f},{y:.1f}")
    stroke = "#1976d2" if kind == "rate" else "#5cb85c"
    return (
        f'<svg class="spark" width="{w:.0f}" height="{h:.0f}" '
        f'viewBox="0 0 {w:.0f} {h:.0f}">'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="1.5" '
        f'points="{" ".join(coords)}"/></svg>'
    )


# ---- ETA (interpretation, not a promise) -----------------------------------

def _eta(done, expected, daily_rate):
    if not daily_rate or daily_rate <= 0 or done is None or expected is None:
        return None
    remaining = expected - done
    if remaining <= 0:
        return "complete"
    days = remaining / daily_rate
    return f"~{days:,.0f} days at the current rate".replace(",", " ")


# ---- an alert-zero card (invariant guard: the count must stay 0) ------------

def _alert_card(m):
    """Card for a `kind: alert_zero` metric: a count that must always be 0.

    0 → OK (green); > 0 → ALERT (red). No percentage, no coverage bar; the
    sparkline shows the raw count history (should be a flat line on zero).
    """
    count = m.get("done") or 0
    alert = count > 0
    status_txt = "ALERT" if alert else "OK"
    color = "#d9534f" if alert else "#5cb85c"
    trend = m.get("trend") or []
    return f"""
    <section class="card{' card-alert' if alert else ''}">
      <div class="card-head">
        <h3>{html.escape(m['description'])}</h3>
        <span class="metric-key">{html.escape(m['key'])}</span>
      </div>
      <div class="pct" style="color:{color}">{_fmt_int(count)}</div>
      <div class="status" style="color:{color}">{status_txt}
        <span class="muted">· target 0</span></div>
      <div class="trend">
        {_sparkline(trend, 'count')}
        <span class="trend-label">count history (should stay 0)</span>
      </div>
      <p class="long-desc">{html.escape(m.get('long_desc') or '')}</p>
    </section>"""


# ---- a pipeline timeline card (a multi-step batch job) ---------------------

_STEP_STYLE = {
    "done":    ("✓", "#5cb85c"),
    "running": ("▶", "#1976d2"),
    "failed":  ("✗", "#d9534f"),
    "pending": ("•", "#b0bec5"),
}


def _pipeline_card(m):
    """Full-width card: an ordered step timeline for a multi-step batch job."""
    overall = m.get("overall_status", "UNKNOWN")
    ostyle = {"SUCCESS": "#5cb85c", "RUNNING": "#1976d2",
              "FAILURE": "#d9534f"}.get(overall, "#90a4ae")
    done = m.get("done") or 0
    total = m.get("expected") or len(m.get("steps") or [])
    rows = []
    for st in m.get("steps") or []:
        icon, color = _STEP_STYLE.get(st["state"], _STEP_STYLE["pending"])
        timing = ""
        if st.get("started"):
            timing = html.escape(st["started"][11:16])  # HH:MM
            if st.get("finished"):
                timing += " → " + html.escape(st["finished"][11:16])
            elif st["state"] == "running":
                timing += " → …"
        dur = f'<span class="step-dur">{html.escape(st["duration"])}</span>' if st.get("duration") else ""
        rows.append(
            f'<li class="step step-{st["state"]}">'
            f'<span class="step-icon" style="color:{color}">{icon}</span>'
            f'<span class="step-code">{st["code"]}</span>'
            f'<span class="step-label">{html.escape(st["label"])}</span>'
            f'<span class="step-timing">{timing}</span>{dur}'
            f'</li>'
        )
    meta_bits = [f'<strong style="color:{ostyle}">{html.escape(overall)}</strong>',
                 f'{done}/{total} steps done']
    if m.get("current_process"):
        meta_bits.append("current: " + html.escape(m["current_process"]))
    if m.get("runtime"):
        meta_bits.append("runtime: " + html.escape(str(m["runtime"])))
    if m.get("started_at"):
        meta_bits.append("started " + html.escape(str(m["started_at"])))
    err = (f'<div class="step-err">last error: {html.escape(str(m["last_error"]))}</div>'
           if m.get("last_error") else "")
    return f"""
    <section class="card card-pipeline{' card-alert' if m.get('alert') else ''}">
      <div class="card-head">
        <h3>{html.escape(m['description'])}</h3>
        <span class="metric-key">{html.escape(m['key'])}</span>
      </div>
      <div class="pipeline-meta">{' &nbsp;·&nbsp; '.join(meta_bits)}</div>
      {_coverage_bar(m.get('pct'), 0)}
      {err}
      <ol class="steps">{''.join(rows)}</ol>
      <p class="long-desc">{html.escape(m.get('long_desc') or '')}</p>
    </section>"""


# ---- a metric card ---------------------------------------------------------

def _metric_card(m):
    if m.get("kind") == "pipeline":
        return _pipeline_card(m)
    if m.get("kind") == "alert_zero":
        return _alert_card(m)
    pct = m.get("pct")
    warn = m.get("warn_below", 50)
    eta = _eta(m.get("done"), m.get("expected"), m.get("daily_rate"))
    trend = m.get("trend") or []
    kind = m.get("trend_kind", "pct")
    # A manifest may name the unit explicitly (`rate_label:`); otherwise fall back
    # to the original key-sniffing default.
    rate_label = m.get("rate_label") or (
        "episodes/day" if "episode" in m["key"] else (
            "seasons/day" if "season" in m["key"] else "per day"))
    return f"""
    <section class="card">
      <div class="card-head">
        <h3>{html.escape(m['description'])}</h3>
        <span class="metric-key">{html.escape(m['key'])}</span>
      </div>
      <div class="pct">{_fmt_pct(pct)}</div>
      {_coverage_bar(pct, warn)}
      <div class="counts">
        <strong>{_fmt_int(m.get('done'))}</strong> done
        / {_fmt_int(m.get('expected'))} expected
        &nbsp;·&nbsp; +{_fmt_int(m.get('daily_rate'))} {rate_label}
        {('&nbsp;·&nbsp; ' + html.escape(eta) + ' <span class="muted">(rough projection)</span>') if eta else ''}
      </div>
      <div class="trend">
        {_sparkline(trend, kind)}
        <span class="trend-label">{'daily rate' if kind == 'rate' else 'completion % history'}</span>
      </div>
      <p class="long-desc">{html.escape(m.get('long_desc') or '')}</p>
    </section>"""


# ---- full document ---------------------------------------------------------

def _alert_banner(metrics):
    """A prominent page-top banner for breached invariants or a failed pipeline."""
    zeros = [m for m in metrics if m.get("kind") == "alert_zero" and (m.get("done") or 0) > 0]
    fails = [m for m in metrics if m.get("kind") == "pipeline" and m.get("alert")]
    if not zeros and not fails:
        return ""
    items = []
    for m in zeros:
        items.append(
            f"<li><strong>{_fmt_int(m.get('done'))}</strong> · "
            f"{html.escape(m['description'])} <code>({html.escape(m['key'])})</code></li>")
    for m in fails:
        err = f" — {html.escape(str(m['last_error']))}" if m.get("last_error") else ""
        items.append(
            f"<li><strong>FAILURE</strong> · {html.escape(m['description'])} "
            f"<code>({html.escape(m['key'])})</code>{err}</li>")
    n = len(zeros) + len(fails)
    note = ("Invariant counts must stay 0 and pipelines must not fail. Investigate the "
            "upstream job (tmdb-crawler / wikidata-crawler) before it spreads.")
    return (
        '<div class="alert-banner" role="alert">'
        f'<div class="alert-title">⚠ ALERT — {n} issue{"s" if n > 1 else ""}</div>'
        f'<ul>{"".join(items)}</ul>'
        f'<div class="alert-note">{note}</div>'
        '</div>'
    )


def render_report(report, metrics, generated_at, db_label):
    cards = "\n".join(_metric_card(m) for m in metrics)
    banner = _alert_banner(metrics)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(report['title'])} — {html.escape(generated_at)}</title>
<style>
  :root {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }}
  body {{ margin: 0; background: #f5f7f9; color: #263238; }}
  header {{ background: #263238; color: #fff; padding: 20px 28px; }}
  header h1 {{ margin: 0 0 4px; font-size: 20px; }}
  header .meta {{ font-size: 12px; color: #b0bec5; }}
  .intro {{ padding: 16px 28px; font-size: 14px; color: #455a64; max-width: 70ch; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
           gap: 16px; padding: 0 28px 28px; }}
  .card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; }}
  .card-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }}
  .card-head h3 {{ margin: 0; font-size: 15px; }}
  .metric-key {{ font-size: 11px; color: #90a4ae; font-family: ui-monospace, monospace; }}
  .pct {{ font-size: 30px; font-weight: 700; margin: 8px 0 4px; }}
  .bar {{ display: block; }}
  .counts {{ font-size: 13px; color: #455a64; margin: 8px 0; }}
  .trend {{ display: flex; align-items: center; gap: 8px; margin: 8px 0; }}
  .trend-label {{ font-size: 11px; color: #90a4ae; }}
  .long-desc {{ font-size: 12px; color: #607d8b; margin: 8px 0 0; line-height: 1.4; }}
  .muted {{ color: #90a4ae; font-size: 11px; }}
  .status {{ font-size: 15px; font-weight: 700; margin: 0 0 8px; }}
  .card-alert {{ border: 2px solid #d9534f; box-shadow: 0 0 0 3px rgba(217,83,79,.12); }}
  .alert-banner {{ margin: 16px 28px; padding: 14px 18px; background: #fdecea;
                   border: 1px solid #d9534f; border-left: 6px solid #d9534f; border-radius: 6px;
                   color: #7f231f; }}
  .alert-banner .alert-title {{ font-size: 15px; font-weight: 700; }}
  .alert-banner ul {{ margin: 8px 0; padding-left: 20px; font-size: 13px; }}
  .alert-banner .alert-note {{ font-size: 12px; color: #a4433f; }}
  .card-pipeline {{ grid-column: 1 / -1; }}
  .pipeline-meta {{ font-size: 13px; color: #455a64; margin: 4px 0 10px; }}
  .steps {{ list-style: none; margin: 10px 0 0; padding: 0; }}
  .step {{ display: flex; align-items: baseline; gap: 10px; padding: 5px 6px;
           border-bottom: 1px solid #eceff1; font-size: 13px; }}
  .step-icon {{ font-weight: 700; width: 14px; text-align: center; flex: none; }}
  .step-code {{ color: #90a4ae; font-family: ui-monospace, monospace; font-size: 11px;
                width: 26px; flex: none; }}
  .step-label {{ flex: 1 1 auto; }}
  .step-timing {{ color: #607d8b; font-size: 12px; font-variant-numeric: tabular-nums; }}
  .step-dur {{ color: #90a4ae; font-size: 11px; min-width: 52px; text-align: right; }}
  .step-running .step-label {{ font-weight: 600; color: #1976d2; }}
  .step-pending {{ opacity: .55; }}
  .step-failed .step-label {{ font-weight: 600; color: #d9534f; }}
  .step-err {{ font-size: 12px; color: #7f231f; margin: 6px 0; }}
  footer {{ padding: 16px 28px; font-size: 11px; color: #90a4ae; }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(report['title'])}</h1>
  <div class="meta">Generated {html.escape(generated_at)} · {html.escape(db_label)} · report slug <code>{html.escape(report['slug'])}</code></div>
</header>
<div class="intro">{html.escape(report.get('description', ''))}</div>
{banner}
<div class="grid">
{cards}
</div>
<footer>
  data-monitoring · read-only snapshot · history in <code>T_WC_DATA_MONITORING_SNAPSHOT</code>.
  Percentages are an upper bound on what upstream exposes; a NULL/zero usually means the
  source has no value, not a crawler miss. ETA is an interpretation, not a commitment.
</footer>
</body>
</html>
"""
