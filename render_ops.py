"""HTML for the ops dashboard (ops.py). Inline CSS, no JS, no CDN: the page opens
offline and archives cleanly, like every data-monitoring report. Collapsible
details use <details>, which needs no script."""
import datetime
import html

import ops

LEVEL_COLOR = {"red": "#d9534f", "amber": "#f0ad4e", "green": "#2e7d32",
               "blue": "#1976d2", "grey": "#90a4ae"}
LEVEL_BG = {"red": "#fdecea", "amber": "#fff8e1", "green": "#e8f5e9",
            "blue": "#e3f2fd", "grey": "#eceff1"}


def e(s):
    return html.escape("" if s is None else str(s))


def _dt(d):
    return d.strftime("%a %d %b %H:%M") if d else "-"


def chip(verdict):
    v = ops.VERDICTS[verdict]
    return (f'<span class="chip" style="background:{LEVEL_BG[v["level"]]};'
            f'color:{LEVEL_COLOR[v["level"]]};border-color:{LEVEL_COLOR[v["level"]]}" '
            f'title="{e(v["help"])}">{e(verdict)}</span>')


def strip(hist_for, today, existing_pages, days=30):
    """30 small squares, oldest left; each links to that day's ops page when it exists."""
    cells = []
    for i in range(days - 1, -1, -1):
        d = today - datetime.timedelta(days=i)
        ds = str(d)
        if ds in hist_for:
            code, desc = hist_for[ds]
            verdict = ops.CODE_TO_VERDICT.get(code, "UNKNOWN")
            color = LEVEL_COLOR[ops.VERDICTS[verdict]["level"]]
            title = f"{ds}: {desc or verdict}"
        else:
            color, title = "#e0e0e0", f"{ds}: no snapshot"
        page = f"ops-{d.strftime('%Y%m%d')}.html"
        cell = f'<span class="cell" style="background:{color}" title="{e(title)}"></span>'
        if page in existing_pages:
            cell = f'<a href="{page}">{cell}</a>'
        cells.append(cell)
    return f'<span class="strip">{"".join(cells)}</span>'


def _log_details(log, title="cron log"):
    if not log or not log.get("exists"):
        return ""
    errs = log.get("errors") or []
    err_html = ""
    if errs:
        err_html = ('<div class="sub">Error-looking lines (last scan):</div><pre class="err">'
                    + e("\n".join(errs)) + "</pre>")
    return (f'<details><summary>{e(title)} · {e(log.get("path"))} · '
            f'{(log.get("size") or 0) / 1024:.0f} KB · modified {e(_short_iso(log.get("mtime")))}'
            f'{" · " + str(log.get("error_count")) + " error line(s)" if log.get("error_count") else ""}'
            f'</summary>{err_html}<div class="sub">Last lines:</div><pre>'
            + e("\n".join(log.get("tail") or [])) + "</pre></details>")


def _short_iso(s):
    d = ops.parse_iso_to_paris(s)
    return _dt(d) if d else (s or "-")


def _attention_card(t, kind):
    v = ops.VERDICTS[t["verdict"]]
    lines = [f'<div class="att-help">{e(v["help"])}</div>']
    if kind == "task":
        facts = []
        if t.get("start"):
            facts.append(f"started {_dt(t['start'])}")
        if t.get("end"):
            facts.append(f"finished {_dt(t['end'])}")
        if t.get("current"):
            facts.append(f"last process: <b>{e(t['current'])}</b>")
        if t.get("status"):
            facts.append(f"status: <b>{e(t['status'])}</b>")
        if facts:
            lines.append('<div class="att-facts">' + " · ".join(facts) + "</div>")
        for n, val in t.get("errors") or []:
            lines.append(f'<div class="att-err"><code>{e(n)}</code> = {e(val[:300])}</div>')
        c = t.get("container_info") or {}
        if c and not c.get("running") and c.get("exit_code"):
            lines.append(f'<div class="att-facts">container {e(c.get("name"))}: exit code '
                         f'{e(c.get("exit_code"))}{", OOM-killed" if c.get("oom_killed") else ""}, '
                         f'finished {_short_iso(c.get("finished_at"))}</div>')
    for n in t.get("notes") or []:
        lines.append(f'<div class="att-note">{e(n)}</div>')
    if kind == "task" and t.get("resume") and t["verdict"] in ("CRASHED", "FAILED"):
        lines.append(f'<div class="att-resume">How to resume: <code>{e(t["resume"])}</code></div>')
    if kind == "task" and t.get("doc"):
        lines.append(f'<div class="att-facts">Runbook: {e(t["doc"])}</div>')
    if kind == "task":
        lines.append(_log_details(t.get("log")))
    if kind == "service":
        c = t.get("container_info") or {}
        if c.get("log_errors"):
            lines.append('<details><summary>error lines in the last 24 h of logs</summary><pre class="err">'
                         + e("\n".join(c["log_errors"])) + "</pre></details>")
    return (f'<div class="att" style="border-left-color:{LEVEL_COLOR[v["level"]]}">'
            f'<div class="att-head">{chip(t["verdict"])} <b>{e(t["label"])}</b>'
            f'<span class="muted"> {e(t.get("group"))}</span></div>{"".join(lines)}</div>')


def _task_rows(tasks, hist, today, pages, now):
    rows = []
    group = None
    for t in sorted(tasks, key=lambda x: (x["group"], x["label"])):
        if t["group"] != group:
            group = t["group"]
            rows.append(f'<tr class="grp"><td colspan="7">{e(group)}</td></tr>')
        cron = t.get("cron") or {}
        if t.get("on_demand"):
            sched = "on demand"
        else:
            sched = ops.human_cron(cron.get("schedule")) or (
                f"every {t['cadence_hours']} h" if t.get("cadence_hours") else "manual")
        last = t.get("end") or t.get("start")
        cur = f'<div class="muted">{e(t["current"][:90])}</div>' if t.get("current") and t["verdict"] != "OK" else ""
        rows.append(
            f'<tr><td>{chip(t["verdict"])}</td>'
            f'<td><b>{e(t["label"])}</b>{cur}</td>'
            f'<td class="num">{_dt(t.get("start"))}</td>'
            f'<td class="num">{e(ops.fmt_duration(t.get("duration")) or "-")}</td>'
            f'<td class="num">{e(ops.fmt_age(now, last) or "-")}</td>'
            f'<td class="muted">{e(sched)}</td>'
            f'<td>{strip(hist.get("task:" + t["slug"], {}), today, pages)}</td></tr>')
    return "\n".join(rows)


def _service_rows(services, hist, today, pages):
    rows = []
    for s in sorted(services, key=lambda x: x["label"]):
        c = s.get("container_info") or {}
        up = ops.parse_iso_to_paris(c.get("started_at")) if c.get("running") else None
        rows.append(
            f'<tr><td>{chip(s["verdict"])}</td><td><b>{e(s["label"])}</b>'
            f'<div class="muted">{e(c.get("name") or ", ".join(s["names"]))}</div></td>'
            f'<td class="num">{_dt(up)}</td>'
            f'<td class="num">{e(c.get("restart_count", "-"))}</td>'
            f'<td>{e(c.get("health") or "-")}</td>'
            f'<td class="num">{e(c.get("log_error_count", "-"))}</td>'
            f'<td>{strip(hist.get("svc:" + s["slug"], {}), today, pages)}</td></tr>')
    return "\n".join(rows)


def _check_tiles(checks):
    out = []
    for c in checks:
        out.append(f'<div class="tile" style="border-top-color:{LEVEL_COLOR[c["level"]]}">'
                   f'<div class="tile-label">{e(c["label"])}</div>'
                   f'<div class="tile-value" style="color:{LEVEL_COLOR[c["level"]]}">{e(c["value"])}</div>'
                   f'<div class="muted">{e(c.get("detail"))}</div></div>')
    return "".join(out)


def _other_cron(entries, now):
    if not entries:
        return '<p class="muted">Every crontab line is claimed by a task above.</p>'
    rows = []
    for c in entries:
        log = c.get("log") or {}
        state = "commented out" if not c.get("enabled") else ("CRON_OFF" if c.get("cron_off") else "active")
        rows.append(f'<tr><td class="muted">{e(ops.human_cron(c.get("schedule")))}</td>'
                    f'<td><code>{e(c.get("command", "")[:140])}</code>{_log_details(log)}</td>'
                    f'<td>{e(state)}</td><td class="num">{e(log.get("error_count", "-"))}</td></tr>')
    return ('<div class="tw"><table><tr><th>When</th><th>Command</th><th>State</th>'
            '<th>Errors</th></tr>' + "".join(rows) + "</table></div>")


def _other_containers(items):
    if not items:
        return ""
    rows = []
    for o in items:
        c = o["info"]
        when = ops.parse_iso_to_paris(c.get("started_at") if c.get("running") else c.get("finished_at"))
        style = ' style="color:#d9534f;font-weight:600"' if o["bad"] else ""
        rows.append(f'<tr><td{style}>{e(o["name"])}</td><td>{e(c.get("status"))}</td>'
                    f'<td class="num">{e(c.get("exit_code"))}</td><td class="num">{_dt(when)}</td>'
                    f'<td class="muted">{e(c.get("image"))}</td></tr>')
    return ('<details><summary>Other containers on the host (' + str(len(items)) + ')</summary>'
            '<div class="tw"><table><tr><th>Name</th><th>State</th><th>Exit</th><th>Since</th>'
            '<th>Image</th></tr>' + "".join(rows) + "</table></div></details>")


def render(data, generated_at, nav_html, pages):
    tasks, services, checks = data["tasks"], data["services"], data["checks"]
    now = data["now"]
    today = now.date()
    attention_t = [t for t in tasks if ops.VERDICTS[t["verdict"]]["level"] in ("red", "amber")]
    attention_s = [s for s in services if ops.VERDICTS[s["verdict"]]["level"] in ("red", "amber")]
    bad_checks = [c for c in checks if c["level"] in ("red", "amber")]
    suspect_cron = [c for c in data["other_cron"] if c.get("suspect")]
    bad_containers = [o for o in data["other_containers"] if o["bad"]]
    n_red = sum(1 for x in attention_t + attention_s if ops.VERDICTS[x["verdict"]]["level"] == "red") \
        + sum(1 for c in bad_checks if c["level"] == "red")
    n_red += len(bad_containers)
    n_amber = len(attention_t) + len(attention_s) + len(bad_checks) + len(suspect_cron)         + len(bad_containers) - n_red
    running = [t for t in tasks if t["verdict"] == "RUNNING"]

    if n_red:
        headline, hl = f"{n_red} problem(s) to investigate, {n_amber} to look at", "red"
    elif n_amber:
        headline, hl = f"Nothing broken, {n_amber} thing(s) to look at", "amber"
    else:
        headline, hl = (f"All good: {len(tasks)} tasks and {len(services)} services healthy", "green")
    sub = []
    if running:
        sub.append("running now: " + ", ".join(t["label"] for t in running))
    off = [t for t in tasks if t["verdict"] == "OFF"]
    if off:
        sub.append("switched off: " + ", ".join(t["label"] for t in off))

    host_warn = ""
    if data.get("host_problem"):
        host_warn = (f'<div class="att" style="border-left-color:{LEVEL_COLOR["amber"]}">'
                     f'<b>Host collector</b><div class="att-note">{e(data["host_problem"])}</div></div>')

    att_html = "".join(_attention_card(t, "task") for t in attention_t) + \
        "".join(_attention_card(s, "service") for s in attention_s)
    if bad_checks:
        att_html += ('<div class="att" style="border-left-color:#f0ad4e"><b>Host and platform</b>'
                     + "".join(f'<div class="att-note">{e(c["label"])}: {e(c["value"])} '
                               f'<span class="muted">{e(c.get("detail"))}</span></div>' for c in bad_checks)
                     + "</div>")
    for c in suspect_cron:
        att_html += (f'<div class="att" style="border-left-color:{LEVEL_COLOR["amber"]}">'
                     f'<div class="att-head">{chip("WARN")} <b>Unregistered cron job</b></div>'
                     f'<div class="att-facts"><code>{e(c.get("command", "")[:160])}</code></div>'
                     f'<div class="att-note">its log ends with error lines; add it to ops/tasks.yaml to follow it</div>'
                     f'{_log_details(c.get("log"))}</div>')
    for o in bad_containers:
        ci = o["info"]
        att_html += (f'<div class="att" style="border-left-color:{LEVEL_COLOR["red"]}">'
                     f'<div class="att-head">{chip("FAILED")} <b>Container {e(o["name"])}</b></div>'
                     f'<div class="att-facts">exited with code {e(ci.get("exit_code"))}'
                     f'{", OOM-killed" if ci.get("oom_killed") else ""} at {_short_iso(ci.get("finished_at"))}'
                     f' (code 137 = killed, often out of memory)</div>'
                     f'<div class="att-note">read it with: <code>docker logs --tail 200 {e(o["name"])}</code></div></div>')
    if not att_html:
        att_html = '<p class="muted">Nothing needs attention.</p>'

    legend = "".join(f'<div>{chip(k)} {e(v["help"])}</div>'
                     for k, v in sorted(ops.VERDICTS.items(), key=lambda kv: kv[1]["rank"]))
    collected = _dt(data.get("collected_at"))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Automated tasks - {e(generated_at)}</title>
<style>
  :root {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }}
  body {{ margin: 0; background: #f5f7f9; color: #263238; }}
  header {{ background: #263238; color: #fff; padding: 18px 28px; }}
  header h1 {{ margin: 0 0 4px; font-size: 20px; }}
  header .meta {{ font-size: 12px; color: #b0bec5; }}
  main {{ padding: 0 28px 28px; max-width: 1280px; }}
  h2 {{ font-size: 16px; margin: 26px 0 10px; }}
  .headline {{ margin: 18px 0 6px; padding: 14px 18px; border-radius: 8px; font-size: 18px;
               font-weight: 700; border: 1px solid; }}
  .headline-sub {{ font-size: 13px; color: #546e7a; margin: 0 0 6px; }}
  .chip {{ display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 7px;
           border-radius: 10px; border: 1px solid; letter-spacing: .02em; white-space: nowrap; }}
  .att {{ background: #fff; border: 1px solid #e0e0e0; border-left: 6px solid; border-radius: 6px;
          padding: 10px 14px; margin: 0 0 10px; font-size: 13px; }}
  .att-head {{ font-size: 14px; margin-bottom: 4px; }}
  .att-help {{ color: #607d8b; font-size: 12px; margin-bottom: 4px; }}
  .att-facts, .att-note {{ margin: 3px 0; }}
  .att-err {{ margin: 3px 0; color: #7f231f; }}
  .att-resume {{ margin: 6px 0; padding: 6px 8px; background: #fff8e1; border-radius: 4px; }}
  .tw {{ overflow-x: auto; background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #eceff1; vertical-align: top; }}
  th {{ font-size: 11px; color: #78909c; text-transform: uppercase; letter-spacing: .04em; }}
  tr.grp td {{ background: #eceff1; font-weight: 700; font-size: 12px; color: #455a64; }}
  td.num {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .muted {{ color: #90a4ae; font-size: 11px; }}
  .strip {{ display: inline-flex; gap: 2px; }}
  .cell {{ display: inline-block; width: 7px; height: 16px; border-radius: 2px; }}
  .tiles {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }}
  .tile {{ background: #fff; border: 1px solid #e0e0e0; border-top: 4px solid; border-radius: 6px; padding: 10px 12px; }}
  .tile-label {{ font-size: 12px; color: #607d8b; }}
  .tile-value {{ font-size: 18px; font-weight: 700; margin: 2px 0; }}
  details {{ margin: 6px 0; font-size: 12px; }}
  summary {{ cursor: pointer; color: #1976d2; }}
  pre {{ background: #263238; color: #eceff1; padding: 8px 10px; border-radius: 4px; overflow-x: auto;
         font-size: 11px; line-height: 1.4; max-height: 360px; white-space: pre-wrap; word-break: break-all; }}
  pre.err {{ background: #3e1f1f; color: #ffcdd2; }}
  .sub {{ font-size: 11px; color: #78909c; margin-top: 6px; }}
  code {{ font-family: ui-monospace, monospace; font-size: 12px; }}
  .legend {{ font-size: 12px; color: #546e7a; display: grid; gap: 4px; }}
  .day-nav {{ display: flex; align-items: center; justify-content: space-between; gap: 12px;
              padding: 10px 28px; background: #eceff1; border-bottom: 1px solid #cfd8dc; flex-wrap: wrap; }}
  .day-nav-days {{ display: flex; align-items: center; gap: 14px; font-size: 13px; }}
  .nav-link {{ color: #1976d2; text-decoration: none; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .nav-disabled {{ color: #b0bec5; font-weight: 400; }}
  .nav-today {{ color: #263238; font-weight: 700; }}
  .nav-index {{ font-size: 12px; color: #607d8b; text-decoration: none; }}
  footer {{ padding: 16px 28px; font-size: 11px; color: #90a4ae; }}
  @media (max-width: 640px) {{ main {{ padding: 0 16px 20px; }} header {{ padding: 14px 16px; }}
    .day-nav {{ padding: 8px 16px; }} .headline {{ font-size: 16px; }} }}
</style>
</head>
<body>
<header>
  <h1>Automated tasks</h1>
  <div class="meta">Generated {e(generated_at)} · host data collected {e(collected)} · report slug <code>{ops.REPORT_SLUG}</code></div>
</header>
{nav_html}
<main>
<div class="headline" style="background:{LEVEL_BG[hl]};color:{LEVEL_COLOR[hl]};border-color:{LEVEL_COLOR[hl]}">{e(headline)}</div>
<div class="headline-sub">{e(" · ".join(sub))}</div>
{host_warn}
<h2>Needs attention</h2>
{att_html}
<h2>Scheduled tasks</h2>
<div class="tw"><table>
<tr><th>Status</th><th>Task</th><th>Last start</th><th>Duration</th><th>Age</th><th>Schedule</th><th>Last 30 days</th></tr>
{_task_rows(tasks, data["history"], today, pages, now)}
</table></div>
<h2>Services that must stay up</h2>
<div class="tw"><table>
<tr><th>Status</th><th>Service</th><th>Up since</th><th>Restarts</th><th>Health</th><th>Log errors 24 h</th><th>Last 30 days</th></tr>
{_service_rows(services, data["history"], today, pages)}
</table></div>
<h2>Host and platform</h2>
<div class="tiles">{_check_tiles(checks)}</div>
<h2>Other cron jobs</h2>
{_other_cron(data["other_cron"], now)}
{_other_containers(data["other_containers"])}
<h2>How to read this page</h2>
<div class="legend">{legend}
<div>Each square of a 30-day strip is one morning check; click it to open that day's page.
A task's verdict comes from its server variables (start, end, status, error variables), its
container and its cron log. Pages are kept {e(data.get("retention", 30))} days here and archived on the NAS; the
verdicts stay in <code>T_WC_DATA_MONITORING_SNAPSHOT</code> for good.</div></div>
</main>
<footer>data-monitoring · ops dashboard · read-only on the platform. Registry: <code>ops/tasks.yaml</code>.</footer>
</body>
</html>
"""
