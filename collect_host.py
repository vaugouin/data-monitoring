#!/usr/bin/env python3
"""Host-side collector for the ops dashboard (ops.py). Standard library only.

The data-monitoring container cannot see the host crontab, the cron log files or
the Docker daemon, so data-monitoring.sh runs this script ON THE HOST, with the
system python3, just before `docker run`. It writes one JSON file that the
container reads through a read-only mount (/hostops/host.json).

What it gathers, all read-only:
  - the crontab of the user running it (debian): active and commented-out lines,
    the log file each one redirects to, and the CRON_OFF flag next to its script;
  - the tail of every cron log (secrets masked), its size and mtime, and the
    lines that look like errors;
  - every Docker container (`docker ps -a` + `docker inspect`): state, start and
    finish times, exit code, OOM kill, restart count, health;
  - for running containers, the error-looking lines of the last 24 h of logs;
  - host vitals: disk, memory, load, uptime, failed systemd units, reboot flag.

It never writes anywhere but --out, and a failing probe records its error instead
of stopping the run: a partial picture beats no picture on the morning page.

Usage (from data-monitoring.sh):
  python3 collect_host.py --docker-root /home/debian/docker --out hostops/host.json
"""
import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess

ERROR_RE = re.compile(
    r"traceback|exception|\berror\b|\bfailed\b|\bfailure\b|fatal|killed|"
    r"lock wait timeout|deadlock|out of memory|no space left|permission denied|"
    r"segmentation fault|connection refused|timed out",
    re.IGNORECASE,
)
# Lines that match ERROR_RE but are routine: counters, "0 errors", our own skips.
BENIGN_RE = re.compile(
    r"\b0 (errors?|failed|failures)\b|error(s)?: ?0\b|errors=0|failed=0|"
    r"error variables? empty|_error\b.*''|SKIPPED",
    re.IGNORECASE,
)
SECRET_RE = re.compile(
    r"((?:password|passwd|pwd|secret|token|api[_-]?key|apikey|authorization|bearer)"
    r"[\"']?\s*[:=]\s*[\"']?|Bearer\s+)([^\s\"',;]+)",
    re.IGNORECASE,
)
KEY_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|ghp_[A-Za-z0-9]{20,})")
CRON_RE = re.compile(r"^\s*(#\s*)?((?:@\w+)|(?:[\d*/,\-]+\s+){4}[\d*/,\-A-Za-z]+)\s+(.+)$")

TAIL_LINES = 60
SCAN_LINES = 400
MAX_ERRORS = 8
DOCKER_LOG_TAIL = 3000


def _mask(line):
    line = SECRET_RE.sub(lambda m: m.group(1) + "***", line)
    return KEY_RE.sub("***", line)


def _run(cmd, timeout=30):
    """(returncode, stdout, stderr); never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:  # missing binary, timeout...
        return -1, "", str(exc)


def _tail(path, nlines):
    """Last nlines of a text file without reading a huge log into memory."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 65536
            data = b""
            pos = size
            while pos > 0 and data.count(b"\n") <= nlines:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-nlines:]
    except OSError:
        return []


def _error_lines(lines):
    hits = [l for l in lines if ERROR_RE.search(l) and not BENIGN_RE.search(l)]
    return len(hits), [_mask(l)[:400] for l in hits[-MAX_ERRORS:]]


def _iso_mtime(path):
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(path)).astimezone().isoformat()
    except OSError:
        return None


def _log_info(path):
    info = {"path": path, "exists": os.path.isfile(path)}
    if not info["exists"]:
        return info
    info["size"] = os.path.getsize(path)
    info["mtime"] = _iso_mtime(path)
    scan = _tail(path, SCAN_LINES)
    info["tail"] = [_mask(l)[:400] for l in scan[-TAIL_LINES:]]
    info["error_count"], info["errors"] = _error_lines(scan)
    return info


def collect_crontab():
    rc, out, err = _run(["crontab", "-l"])
    if rc != 0:
        return {"error": (err or out).strip() or f"crontab -l exit {rc}", "entries": []}
    entries = []
    for raw in out.splitlines():
        m = CRON_RE.match(raw)
        if not m:
            continue
        commented, schedule, line = bool(m.group(1)), m.group(2).strip(), m.group(3).strip()
        # A commented line that is prose, not a job, has no path in it.
        if commented and "/" not in line:
            continue
        # One cron line may chain several jobs (`a.sh >> a.log ; b.sh >> b.log`):
        # each gets its own entry, log and CRON_OFF check.
        for command in [c.strip() for c in re.split(r"\s;\s|\s&&\s", line) if c.strip()]:
            entries.append(_cron_entry(schedule, command, commented))
    return {"entries": entries}


def _cron_entry(schedule, command, commented):
    log_path = None
    lm = re.search(r">>?\s*(\S+)", command)
    if lm and lm.group(1) not in ("/dev/null", "&1"):
        log_path = os.path.expanduser(lm.group(1))
    script = None
    try:
        parts = shlex.split(command.split(">")[0])
    except ValueError:
        parts = command.split()
    for tok in parts:
        if tok.startswith("/") or tok.startswith("~") or tok.endswith((".sh", ".py")):
            if tok.endswith((".sh", ".py")):
                script = os.path.expanduser(tok)
                break
    entry = {"schedule": schedule, "command": _mask(command), "enabled": not commented,
             "script": script, "log": None, "cron_off": None}
    if script:
        flag = os.path.join(os.path.dirname(script), "CRON_OFF")
        if os.path.isfile(flag):
            try:
                with open(flag, encoding="utf-8", errors="replace") as fh:
                    entry["cron_off"] = fh.read().strip()[:200] or "present"
            except OSError:
                entry["cron_off"] = "present"
    if log_path:
        entry["log"] = _log_info(log_path)
    return entry


def collect_extra_logs(listfile):
    """Logs no crontab line redirects to (a job's own run log, the root crontab's
    TLS renewal log...), listed one path or glob per line in ops/extra-logs.txt.
    For a glob, the newest match is read."""
    import glob as _glob
    out = {}
    try:
        with open(listfile, encoding="utf-8") as fh:
            patterns = [l.strip() for l in fh if l.strip() and not l.lstrip().startswith("#")]
    except OSError as exc:
        return {"_error": str(exc)}
    for pat in patterns:
        # "mtime:<glob>" = freshness only (newest file's mtime and size, no content):
        # for outputs such as CSV exports, whose lines are data, not a log.
        stat_only = pat.startswith("mtime:")
        glob_pat = pat[6:].strip() if stat_only else pat
        matches = sorted(_glob.glob(os.path.expanduser(glob_pat)), key=lambda p: os.path.getmtime(p))
        if not matches:
            out[pat] = {"path": glob_pat, "exists": False}
        elif stat_only:
            out[pat] = {"path": matches[-1], "exists": True, "size": os.path.getsize(matches[-1]),
                        "mtime": _iso_mtime(matches[-1]), "files": len(matches)}
        else:
            out[pat] = _log_info(matches[-1])
    return out


def collect_docker():
    rc, out, err = _run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}"])
    if rc != 0:
        return {"error": (err or out).strip() or f"docker ps exit {rc}", "containers": []}
    ids = [l.strip() for l in out.splitlines() if l.strip()]
    containers = []
    if ids:
        rc, out, err = _run(["docker", "inspect"] + ids, timeout=60)
        try:
            data = json.loads(out) if rc == 0 else []
        except ValueError:
            data = []
        for c in data:
            st = c.get("State") or {}
            health = (st.get("Health") or {}).get("Status")
            item = {
                "name": (c.get("Name") or "").lstrip("/"),
                "image": (c.get("Config") or {}).get("Image"),
                "status": st.get("Status"),
                "running": bool(st.get("Running")),
                "started_at": st.get("StartedAt"),
                "finished_at": st.get("FinishedAt"),
                "exit_code": st.get("ExitCode"),
                "oom_killed": bool(st.get("OOMKilled")),
                "error": st.get("Error") or None,
                "restart_count": c.get("RestartCount", 0),
                "restart_policy": ((c.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name"),
                "health": health,
                "compose_project": ((c.get("Config") or {}).get("Labels") or {}).get(
                    "com.docker.compose.project"),
            }
            if item["running"]:
                lrc, lout, lerr = _run(["docker", "logs", "--since", "24h", "--tail",
                                        str(DOCKER_LOG_TAIL), item["name"]], timeout=30)
                lines = (lout + "\n" + lerr).splitlines() if lrc == 0 else []
                item["log_lines_24h"] = len(lines)
                item["log_error_count"], item["log_errors"] = _error_lines(lines)
            containers.append(item)
    return {"containers": containers}


def collect_host(docker_root):
    host = {}
    disks = []
    for label, path in (("/", "/"), ("docker", docker_root),
                        ("shared_data", os.path.join(docker_root, "shared_data"))):
        if os.path.exists(path):
            try:
                du = shutil.disk_usage(path)
                disks.append({"label": label, "path": path, "total": du.total,
                              "used": du.used, "free": du.free})
            except OSError as exc:
                disks.append({"label": label, "path": path, "error": str(exc)})
    # Distinct filesystems only (/, docker and shared_data are often the same one).
    seen, uniq = set(), []
    for d in disks:
        sig = (d.get("total"), d.get("used"))
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(d)
    host["disks"] = uniq
    try:
        mem = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                mem[k] = int(v.strip().split()[0]) * 1024
        host["memory"] = {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable"),
                          "swap_total": mem.get("SwapTotal"), "swap_free": mem.get("SwapFree")}
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/loadavg") as fh:
            host["load"] = [float(x) for x in fh.read().split()[:3]]
        host["cpus"] = os.cpu_count()
        with open("/proc/uptime") as fh:
            host["uptime_s"] = float(fh.read().split()[0])
    except (OSError, ValueError):
        pass
    rc, out, _ = _run(["systemctl", "--failed", "--no-legend", "--plain"])
    host["failed_units"] = [l.split()[0] for l in out.splitlines() if l.strip()] if rc == 0 else None
    host["reboot_required"] = os.path.exists("/var/run/reboot-required")
    rc, out, _ = _run(["hostname"])
    host["hostname"] = out.strip() if rc == 0 else None
    return host


def main():
    ap = argparse.ArgumentParser(description="ops dashboard host collector")
    ap.add_argument("--docker-root", default=os.path.expanduser("~/docker"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    started = datetime.datetime.now().astimezone()
    result = {"collected_at": started.isoformat(), "version": 1}
    extra = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops", "extra-logs.txt")
    for key, fn in (("crontab", collect_crontab), ("docker", collect_docker),
                    ("host", lambda: collect_host(args.docker_root)),
                    ("extra_logs", lambda: collect_extra_logs(extra))):
        try:
            result[key] = fn()
        except Exception as exc:  # keep the other sections
            result[key] = {"error": f"{type(exc).__name__}: {exc}"}
    result["duration_s"] = round((datetime.datetime.now().astimezone() - started).total_seconds(), 1)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, args.out)
    print(f"collect_host: wrote {args.out} in {result['duration_s']} s")


if __name__ == "__main__":
    main()
