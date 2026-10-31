"""Ops dashboard: is every automated task of the VPS running well?

data-monitoring's other reports answer "how far along is a campaign". This one
answers the morning question: did the night's crawlers, preprocessors and other
background jobs run, finish, crash, or silently stop? It crosses three sources:

  1. T_WC_SERVER_VARIABLE, where each job writes its start, end, status and
     current process (read-only, one small indexed query);
  2. hostops/host.json, written on the host by collect_host.py just before the
     container starts: crontab, cron log tails, `docker ps -a` / `docker inspect`,
     24 h of service logs, disk, memory, load;
  3. the registry ops/tasks.yaml: which job is which, its cadence, its variables,
     its container, its cron line, and the long-running services that must be up.

Each task gets a verdict (CRASHED, FAILED, STALE, WARN, RUNNING, OK, OFF,
UNKNOWN). Verdicts are upserted into T_WC_DATA_MONITORING_SNAPSHOT
(REPORT_SLUG 'ops-health', SOURCE_DB 'OPS'), which gives a 30-day status strip
per task and keeps the history after the HTML is pruned. The page itself is
ops-YYYYMMDD.html (walkable day by day, like every report) plus ops-latest.html,
the stable URL to open each morning.

Clock: job datetimes in server variables are naive Paris (DEPLOYMENT-015);
host.json carries ISO stamps with offsets; everything is shown in Paris time.
"""
import datetime
import json
import os
import re
import socket
import ssl

REPORT_SLUG = "ops-health"
SOURCE_DB = "OPS"
OPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops")
HOSTOPS_FILE = os.environ.get("HOSTOPS_FILE", "/hostops/host.json")

# Verdicts, worst first. The code is what the snapshot stores (DONE_COUNT).
VERDICTS = {
    "CRASHED": {"code": 7, "level": "red", "rank": 0,
                "help": "started, never finished, and its container is gone: read its log"},
    "FAILED": {"code": 6, "level": "red", "rank": 1,
               "help": "the job itself reported a failure"},
    "DOWN": {"code": 8, "level": "red", "rank": 2,
             "help": "a service that must always run is not running"},
    "STALE": {"code": 5, "level": "amber", "rank": 3,
              "help": "no run within its expected cadence: cron missed, or launcher refused to start"},
    "WARN": {"code": 4, "level": "amber", "rank": 4,
             "help": "finished, but with an error variable, error lines or restarts to look at"},
    "LONG": {"code": 9, "level": "amber", "rank": 5,
             "help": "still running, longer than its usual maximum"},
    "RUNNING": {"code": 1, "level": "blue", "rank": 6, "help": "running now"},
    "OK": {"code": 0, "level": "green", "rank": 7, "help": "last run finished normally"},
    "OFF": {"code": 2, "level": "grey", "rank": 8,
            "help": "switched off on purpose (CRON_OFF flag or commented cron line)"},
    "UNKNOWN": {"code": 3, "level": "grey", "rank": 9,
                "help": "no trace found: check the registry entry"},
}
CODE_TO_VERDICT = {v["code"]: k for k, v in VERDICTS.items()}
FAIL_WORDS = ("FAIL", "ERROR", "CRASH", "ABORT", "KILLED")
DT_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)")


# --- small helpers -----------------------------------------------------------

def _paris():
    import pytz
    return pytz.timezone(os.environ.get("USER_TIMEZONE", "Europe/Paris"))


def parse_naive(value):
    """A naive Paris datetime from a server-variable string, or None."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.replace(tzinfo=None)
    m = DT_RE.search(str(value))
    if not m:
        return None
    t = m.group(2) if len(m.group(2)) == 8 else m.group(2) + ":00"
    try:
        return datetime.datetime.strptime(f"{m.group(1)} {t}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_iso_to_paris(value):
    """Docker / host ISO stamp (with offset or Z) to naive Paris; None for the zero date."""
    if not value or str(value).startswith("0001-"):
        return None
    s = str(value).replace("Z", "+00:00")
    # Docker gives 0 to 9 fraction digits (trailing zeros trimmed); Python 3.10
    # only parses exactly 3 or 6, so pad or cut to 6.
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(_paris()).replace(tzinfo=None)


def fmt_duration(seconds):
    if seconds is None or seconds < 0:
        return None
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h:02d}h{m:02d}"
    if h:
        return f"{h}h{m:02d}"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def fmt_age(now, dt):
    if dt is None:
        return None
    return fmt_duration((now - dt).total_seconds())


def human_cron(schedule):
    """'30 6 * * *' -> 'daily 06:30 UTC'. Anything unusual is returned as is."""
    if not schedule:
        return None
    parts = schedule.split()
    if len(parts) != 5:
        return schedule
    mi, h, dom, mon, dow = parts
    if mi.isdigit() and h.isdigit() and dom == mon == "*":
        hhmm = f"{int(h):02d}:{int(mi):02d} UTC"
        if dow == "*":
            return f"daily {hhmm}"
        days = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri",
                "6": "Sat", "7": "Sun"}
        return f"{','.join(days.get(d, d) for d in dow.split(','))} {hhmm}"
    if mi.isdigit() and h == "*" and dom == mon == dow == "*":
        return f"hourly at :{int(mi):02d}"
    if mi.isdigit() and h.startswith("*/") and dom == mon == dow == "*":
        return f"every {h[2:]} h at :{int(mi):02d}"
    if mi.startswith("*/") and h == "*" and dom == mon == dow == "*":
        return f"every {mi[2:]} min"
    if mi.isdigit() and h.isdigit() and dom.isdigit() and mon == dow == "*":
        return f"monthly, day {dom} {int(h):02d}:{int(mi):02d} UTC"
    return schedule


def load_registry():
    import yaml
    with open(os.path.join(OPS_DIR, "tasks.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_host():
    try:
        with open(HOSTOPS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        return {"error": f"host collector output unreadable ({HOSTOPS_FILE}): {exc}"}


# --- task verdicts -------------------------------------------------------------

def _task_var_names(task):
    """The variable map of a task: explicit `vars:` wins over the `prefix:` defaults."""
    names = {}
    prefix = task.get("prefix")
    if prefix:
        names = {
            "start": prefix + "startdatetime",
            "end": prefix + "enddatetime",
            "current": prefix + "currentprocess",
            "runtime": prefix + "totalruntime",
        }
    names.update(task.get("vars") or {})
    return names


def wanted_variables(registry):
    names = set()
    for t in registry.get("tasks") or []:
        for k, v in _task_var_names(t).items():
            if isinstance(v, list):
                names.update(v)
            elif v:
                names.add(v)
    return sorted(names)


def fetch_variables(conn, names):
    if not names:
        return {}
    marks = ",".join(["%s"] * len(names))
    sql = ("SELECT VAR_NAME, VAR_VALUE, TIM_UPDATED FROM T_WC_SERVER_VARIABLE "
           f"WHERE VAR_NAME IN ({marks}) AND (DELETED = 0 OR DELETED IS NULL)")
    with conn.cursor() as cur:
        cur.execute(sql, names)
        rows = cur.fetchall()
    return {r["VAR_NAME"]: (r["VAR_VALUE"], r["TIM_UPDATED"]) for r in rows}


def _cron_entry(task, entries):
    match = task.get("cron_match")
    if not match:
        return None
    hits = [e for e in entries if match in (e.get("command") or "")]
    # An active line beats a commented-out copy of the same job.
    hits.sort(key=lambda e: not e.get("enabled"))
    return hits[0] if hits else None


def _task_log(task, cron, extra_logs):
    """The log to read for a task: an explicit `log:` (from ops/extra-logs.txt) wins
    over the file its cron line redirects to."""
    if task.get("log"):
        return extra_logs.get(task["log"]) or {"path": task["log"], "exists": False}
    return (cron or {}).get("log") or {}


def evaluate_task(task, vars_, cron_entries, containers, now, extra_logs=None):
    names = _task_var_names(task)

    def val(key):
        n = names.get(key)
        if not n or isinstance(n, list):
            return None
        v = vars_.get(n)
        return v[0] if v else None

    start = parse_naive(val("start"))
    end = parse_naive(val("end"))
    status = (val("status") or "").strip()
    current = (val("current") or "").strip()
    runtime = (val("runtime") or "").strip()
    error_vals = []
    for n in names.get("errors") or []:
        v = vars_.get(n)
        if v and v[0] and str(v[0]).strip():
            error_vals.append((n, str(v[0]).strip()))

    cron = _cron_entry(task, cron_entries)
    cname = task.get("container")
    container = containers.get(cname) if cname else None
    running = bool(container and container.get("running"))
    if not running and task.get("container_prefix"):
        for n, c in containers.items():
            if n.startswith(task["container_prefix"]) and c.get("running"):
                running, container = True, c
                break

    # A job that writes no start variable can still be dated by its container.
    if start is None and container:
        start = parse_iso_to_paris(container.get("started_at"))
        if end is None and not running:
            end = parse_iso_to_paris(container.get("finished_at"))

    log = _task_log(task, cron, extra_logs or {})
    log_mtime = parse_iso_to_paris(log.get("mtime")) if log.get("mtime") else None
    # Jobs that write no server variable at all (backups, exports, watchdogs) are
    # dated by their log: the file is touched at every run.
    log_dated = False
    if task.get("log_only") and start is None and log_mtime:
        start = end = log_mtime
        log_dated = True

    cadence_h = task.get("cadence_hours")
    max_run_h = task.get("max_runtime_hours", 12)
    notes = []
    off = False
    if cron:
        if not cron.get("enabled"):
            off = True
            notes.append("cron line commented out")
        if cron.get("cron_off"):
            off = True
            notes.append(f"CRON_OFF: {cron['cron_off']}")
    elif task.get("cron_match") and cron_entries:
        notes.append(f"no crontab line contains '{task['cron_match']}'")

    if log_dated:
        duration = None  # a log's mtime says when, not how long
    elif start and end and end >= start:
        duration = (end - start).total_seconds()
    elif start and running:
        duration = (now - start).total_seconds()
    else:
        duration = None

    finished = bool(start and end and end >= start)
    # Jobs without an end variable (embedding-update) are finished once their
    # totalruntime holds a duration instead of the "RUNNING" written at start.
    if task.get("end_from_runtime") and start and runtime:
        finished = runtime.upper() != "RUNNING"
    # The house idiom writes totalruntime = "RUNNING" at start and a duration at the
    # end, and nothing on a crash: "RUNNING" with no live container is a crash.
    runtime_stuck = runtime.upper() == "RUNNING" and not running
    status_failed = any(w in status.upper() for w in FAIL_WORDS)

    if running:
        verdict = "LONG" if duration and duration > max_run_h * 3600 else "RUNNING"
    elif status_failed:
        verdict = "FAILED"
    elif (start and not finished and not task.get("no_end_marker")) or runtime_stuck:
        verdict = "CRASHED"
        if runtime_stuck:
            notes.append("totalruntime still says RUNNING but no container is running")
    elif container and container.get("oom_killed"):
        verdict = "CRASHED"
        notes.append("container was OOM-killed")
    elif container and not container.get("running") and (container.get("exit_code") or 0) != 0 \
            and task.get("container_exit_counts", True):
        fin = parse_iso_to_paris(container.get("finished_at"))
        if fin and (now - fin).total_seconds() < 36 * 3600:
            verdict = "FAILED"
            notes.append(f"container exited with code {container.get('exit_code')}")
        else:
            verdict = "OK"
    elif start is None:
        verdict = "UNKNOWN"
    else:
        verdict = "OK"

    last = end or start
    if verdict == "OK" and cadence_h and last:
        grace = cadence_h * 3600 * 1.25 + task.get("grace_hours", 3) * 3600
        if (now - last).total_seconds() > grace:
            verdict = "STALE"
    if verdict == "UNKNOWN" and cadence_h is None and not task.get("cron_match"):
        verdict = "UNKNOWN"

    log_recent = bool(log_mtime and (now - log_mtime).total_seconds() < 30 * 3600)
    if error_vals and verdict in ("OK", "STALE"):
        verdict = "WARN" if verdict == "OK" else verdict
    if verdict == "OK" and log_recent and log.get("error_count") and task.get("log_errors_warn", True):
        # Only the lines written since the last start matter, and a cron log has no
        # timestamps: escalate when the very end of the log looks like an error.
        tail = log.get("tail") or []
        window = task.get("log_tail_window", 15)
        if any(e in tail[-window:] for e in (log.get("errors") or [])):
            verdict = "WARN"
            notes.append("the end of its log contains error lines")
    # A job whose log must end on a known success line (a backup, an ingest).
    if verdict == "OK" and task.get("log_ok_pattern") and log.get("tail"):
        if not any(re.search(task["log_ok_pattern"], l) for l in log["tail"][-task.get("log_tail_window", 15):]):
            verdict = "WARN"
            notes.append(f"the end of its log has no line matching '{task['log_ok_pattern']}'")
    if container and container.get("restart_count"):
        notes.append(f"container restarted {container['restart_count']} time(s)")

    if off and verdict in ("OK", "STALE", "UNKNOWN"):
        verdict = "OFF"

    return {
        "slug": task["slug"], "label": task.get("label", task["slug"]),
        "group": task.get("group", "Other"), "repo": task.get("repo"),
        "description": task.get("description"), "verdict": verdict,
        "start": start, "end": end if finished else None, "duration": duration,
        "status": status, "current": current, "runtime": runtime,
        "errors": error_vals, "notes": notes, "running": running,
        "container": cname, "container_info": container,
        "cron": cron, "log": log, "cadence_hours": cadence_h, "last": last,
        "doc": task.get("doc"), "resume": task.get("resume"),
        "on_demand": task.get("on_demand", False),
    }


# --- services, other cron jobs, host -------------------------------------------

def evaluate_services(registry, containers, prev_restarts):
    out = []
    for s in registry.get("services") or []:
        names = [s["container"]] if s.get("container") else []
        if s.get("any_of"):
            names = s["any_of"]
        found = [containers[n] for n in names if n in containers]
        live = [c for c in found if c.get("running")]
        notes = []
        if live:
            c = live[0]
            verdict = "OK"
            if c.get("health") == "unhealthy":
                verdict = "DOWN"
                notes.append("health check: unhealthy")
            prev = prev_restarts.get(c["name"])
            if prev is not None and (c.get("restart_count") or 0) > prev:
                verdict = "WARN" if verdict == "OK" else verdict
                notes.append(f"restarted {(c.get('restart_count') or 0) - prev} time(s) since the last check")
            if (c.get("log_error_count") or 0) >= s.get("log_errors_warn", 200):
                verdict = "WARN" if verdict == "OK" else verdict
                notes.append(f"{c['log_error_count']} error lines in 24 h of logs")
        else:
            c = found[0] if found else None
            verdict = "DOWN" if not s.get("optional") else "OFF"
            if c:
                notes.append(f"state {c.get('status')}, exit code {c.get('exit_code')}")
            else:
                notes.append("no such container")
        out.append({"slug": s.get("slug") or (names[0] if names else "?"),
                    "label": s.get("label") or (names[0] if names else "?"),
                    "group": s.get("group", "Services"), "verdict": verdict,
                    "container_info": c, "names": names, "notes": notes,
                    "description": s.get("description")})
    return out


def other_cron_entries(registry, entries):
    """Crontab lines no task claims, so a new job is never invisible."""
    matches = [t["cron_match"] for t in registry.get("tasks") or [] if t.get("cron_match")]
    ignore = registry.get("cron_ignore") or []
    out = []
    for e in entries:
        cmd = e.get("command") or ""
        if any(m in cmd for m in matches) or any(i in cmd for i in ignore):
            continue
        log = e.get("log") or {}
        tail = log.get("tail") or []
        e["suspect"] = bool(e.get("enabled") and not e.get("cron_off")
                            and any(x in tail[-15:] for x in (log.get("errors") or [])))
        out.append(e)
    return out


def other_containers(registry, containers, now):
    """Containers no task or service claims; one-shot ones that exited badly stand out."""
    claimed = set()
    for t in registry.get("tasks") or []:
        if t.get("container"):
            claimed.add(t["container"])
    for s in registry.get("services") or []:
        claimed.update([s["container"]] if s.get("container") else s.get("any_of") or [])
    prefixes = [t["container_prefix"] for t in registry.get("tasks") or [] if t.get("container_prefix")]
    out = []
    for name, c in sorted(containers.items()):
        if name in claimed or any(name.startswith(p) for p in prefixes):
            continue
        fin = parse_iso_to_paris(c.get("finished_at"))
        recent = bool(fin and (now - fin).total_seconds() < 36 * 3600)
        bad = (not c.get("running")) and recent and ((c.get("exit_code") or 0) != 0 or c.get("oom_killed"))
        out.append({"name": name, "info": c, "bad": bad})
    return out


def host_checks(host, thresholds):
    checks = []
    h = host.get("host") or {}
    for d in h.get("disks") or []:
        if d.get("total"):
            pct = 100.0 * d["used"] / d["total"]
            lvl = "red" if pct >= thresholds.get("disk_red", 95) else \
                  "amber" if pct >= thresholds.get("disk_amber", 85) else "green"
            checks.append({"label": f"Disk {d['label']}", "value": f"{pct:.0f}% used",
                           "detail": f"{d['free'] / 1e9:.0f} GB free of {d['total'] / 1e9:.0f} GB",
                           "level": lvl, "pct": pct, "key": f"disk_{d['label']}"})
    mem = h.get("memory") or {}
    if mem.get("total"):
        avail = 100.0 * (mem.get("available") or 0) / mem["total"]
        lvl = "red" if avail < 5 else "amber" if avail < 12 else "green"
        checks.append({"label": "Memory", "value": f"{avail:.0f}% available",
                       "detail": f"{(mem.get('available') or 0) / 1e9:.1f} GB of {mem['total'] / 1e9:.1f} GB",
                       "level": lvl})
    if mem.get("swap_total"):
        used = 100.0 * (mem["swap_total"] - (mem.get("swap_free") or 0)) / mem["swap_total"]
        checks.append({"label": "Swap", "value": f"{used:.0f}% used", "detail": "",
                       "level": "amber" if used > 60 else "green"})
    if h.get("load") and h.get("cpus"):
        ratio = h["load"][1] / h["cpus"]
        checks.append({"label": "Load (5 min)", "value": f"{h['load'][1]:.2f}",
                       "detail": f"{h['cpus']} CPUs", "level": "amber" if ratio > 1.5 else "green"})
    if h.get("uptime_s") is not None:
        checks.append({"label": "Host uptime", "value": fmt_duration(h["uptime_s"]),
                       "detail": "a short uptime means a reboot last night",
                       "level": "amber" if h["uptime_s"] < 86400 else "green"})
    if h.get("failed_units") is not None:
        fu = h["failed_units"]
        checks.append({"label": "Failed systemd units", "value": str(len(fu)),
                       "detail": ", ".join(fu), "level": "red" if fu else "green"})
    if h.get("reboot_required"):
        checks.append({"label": "Reboot required", "value": "yes",
                       "detail": "security updates wait for a reboot", "level": "amber"})
    return checks


def tls_checks(registry, now_utc):
    out = []
    for host in registry.get("tls_hosts") or []:
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=8) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ss:
                    cert = ss.getpeercert()
            exp = datetime.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
            days = (exp - now_utc).total_seconds() / 86400
            lvl = "red" if days < 7 else "amber" if days < 20 else "green"
            out.append({"label": f"TLS {host}", "value": f"{days:.0f} days left",
                        "detail": f"expires {exp:%Y-%m-%d}", "level": lvl})
        except Exception as exc:
            out.append({"label": f"TLS {host}", "value": "check failed",
                        "detail": f"{type(exc).__name__}: {exc}"[:160], "level": "red"})
    return out


def db_checks(conn):
    out = []
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW GLOBAL STATUS WHERE Variable_name IN "
                        "('Uptime','Threads_connected','Slow_queries','Aborted_connects')")
            st = {r["Variable_name"]: r["Value"] for r in cur.fetchall()}
        up = int(st.get("Uptime", 0))
        out.append({"label": "MariaDB uptime", "value": fmt_duration(up),
                    "detail": f"{st.get('Threads_connected', '?')} connections",
                    "level": "amber" if up < 86400 else "green"})
    except Exception as exc:
        out.append({"label": "MariaDB", "value": "status unreadable", "detail": str(exc)[:160],
                    "level": "amber"})
    return out


# --- snapshot history -------------------------------------------------------------

def _row(table, key, done, expected, desc, long_desc, order, dat, tim):
    return {"REPORT_SLUG": REPORT_SLUG, "SOURCE_DB": SOURCE_DB, "TABLE_NAME": table[:100],
            "METRIC_KEY": key, "DONE_COUNT": done, "EXPECTED_COUNT": expected, "PCT": None,
            "DAILY_RATE": None, "DESCRIPTION": (desc or "")[:250], "LONG_DESC": long_desc,
            "DELETED": 0, "DISPLAY_ORDER": order, "ID_CREATOR": 0, "DAT_CREAT": dat,
            "ID_OWNER": 0, "TIM_UPDATED": tim, "ID_USER_UPDATED": 0}


def history(conn, days):
    """{TABLE_NAME: {date: (code, description)}} over the last `days` days."""
    sql = ("SELECT DAT_CREAT, TABLE_NAME, DONE_COUNT, DESCRIPTION FROM T_WC_DATA_MONITORING_SNAPSHOT "
           "WHERE REPORT_SLUG = %s AND SOURCE_DB = %s AND METRIC_KEY = 'ops_status' "
           "AND DAT_CREAT >= CURDATE() - INTERVAL %s DAY AND DELETED = 0")
    out = {}
    with conn.cursor() as cur:
        cur.execute(sql, (REPORT_SLUG, SOURCE_DB, days))
        for r in cur.fetchall():
            out.setdefault(r["TABLE_NAME"], {})[str(r["DAT_CREAT"])] = (r["DONE_COUNT"], r["DESCRIPTION"])
    return out


def previous_restarts(conn, before):
    sql = ("SELECT TABLE_NAME, EXPECTED_COUNT FROM T_WC_DATA_MONITORING_SNAPSHOT "
           "WHERE REPORT_SLUG = %s AND SOURCE_DB = %s AND METRIC_KEY = 'ops_restarts' "
           "AND DAT_CREAT = (SELECT MAX(DAT_CREAT) FROM T_WC_DATA_MONITORING_SNAPSHOT "
           "WHERE REPORT_SLUG = %s AND METRIC_KEY = 'ops_restarts' AND DAT_CREAT < %s)")
    with conn.cursor() as cur:
        cur.execute(sql, (REPORT_SLUG, SOURCE_DB, REPORT_SLUG, before))
        return {r["TABLE_NAME"][4:]: r["EXPECTED_COUNT"] for r in cur.fetchall()
                if r["TABLE_NAME"].startswith("svc:")}


def persist(conn, tasks, services, checks, run_dt):
    import db
    dat = run_dt.date()
    tim = run_dt.strftime("%Y-%m-%d %H:%M:%S")
    order = 0
    for t in tasks:
        order += 1
        detail = {"start": str(t["start"] or ""), "end": str(t["end"] or ""),
                  "status": t["status"], "current": t["current"], "notes": t["notes"],
                  "errors": t["errors"]}
        db.upsert_snapshot(conn, _row(
            f"task:{t['slug']}", "ops_status", VERDICTS[t["verdict"]]["code"],
            int(t["duration"]) if t["duration"] is not None else None,
            f"{t['verdict']} · {t['label']}", json.dumps(detail, ensure_ascii=False, default=str),
            order, dat, tim))
    for s in services:
        order += 1
        c = s.get("container_info") or {}
        db.upsert_snapshot(conn, _row(f"svc:{s['slug']}", "ops_status", VERDICTS[s["verdict"]]["code"],
                                      None, f"{s['verdict']} · {s['label']}",
                                      json.dumps({"notes": s["notes"]}), order, dat, tim))
        if c.get("name"):
            db.upsert_snapshot(conn, _row(f"svc:{c['name']}", "ops_restarts", None,
                                          c.get("restart_count") or 0, "restart count",
                                          None, order, dat, tim))
    for c in checks:
        if c.get("key") and c.get("pct") is not None:
            order += 1
            db.upsert_snapshot(conn, _row(f"host:{c['key']}", "ops_host_pct", int(round(c["pct"])),
                                          100, c["label"], None, order, dat, tim))


# --- entry point ---------------------------------------------------------------------

def build(conn, run_dt, persist_snapshot=True):
    """Everything the page needs, as one dict (also used by --ops-sample)."""
    registry = load_registry()
    host = load_host()
    now = run_dt.replace(tzinfo=None)
    now_utc = datetime.datetime.utcnow()
    entries = (host.get("crontab") or {}).get("entries") or []
    containers = {c["name"]: c for c in (host.get("docker") or {}).get("containers") or []}

    vars_ = fetch_variables(conn, wanted_variables(registry))
    extra_logs = host.get("extra_logs") or {}
    tasks = [evaluate_task(t, vars_, entries, containers, now, extra_logs)
             for t in registry.get("tasks") or []]
    tasks.sort(key=lambda t: (VERDICTS[t["verdict"]]["rank"], t["group"], t["label"]))
    prev = previous_restarts(conn, run_dt.date())
    services = evaluate_services(registry, containers, prev)
    services.sort(key=lambda s: (VERDICTS[s["verdict"]]["rank"], s["label"]))

    checks = host_checks(host, registry.get("thresholds") or {})
    checks += db_checks(conn)
    checks += tls_checks(registry, now_utc)

    collected = parse_iso_to_paris(host.get("collected_at"))
    host_problem = host.get("error")
    if not host_problem and (collected is None or (now - collected).total_seconds() > 3 * 3600):
        host_problem = (f"host data collected {fmt_age(now, collected) or '?'} ago: "
                        "the cron logs and container states below may be out of date")
    for sect in ("crontab", "docker", "host"):
        err = (host.get(sect) or {}).get("error") if isinstance(host.get(sect), dict) else None
        if err:
            host_problem = (host_problem + "; " if host_problem else "") + f"{sect}: {err}"

    if persist_snapshot:
        persist(conn, tasks, services, checks, run_dt)
    hist = history(conn, 30)
    return {
        "tasks": tasks, "services": services, "checks": checks,
        "other_cron": other_cron_entries(registry, entries),
        "other_containers": other_containers(registry, containers, now),
        "history": hist, "host_problem": host_problem, "collected_at": collected,
        "now": now, "registry": registry,
    }
