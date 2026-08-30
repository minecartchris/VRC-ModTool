"""Everything worth knowing about the container and the mod tool, in one dict.

Standard library only, and read-only throughout: this runs beside a service
holding a 512 MB container, so it has no business allocating much or writing
anything. The database is opened `mode=ro` — a monitor that can corrupt what
it monitors is worse than no monitor.

Used by both `status.sh` (a terminal) and `monitor_web.py` (a page), so the
two can never disagree about what the numbers mean.
"""
import json
import os
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

DB_PATH = os.environ.get("MODTOOL_DB", "/var/lib/modsuite/modtool.db")
SERVICE = os.environ.get("MODSUITE_SERVICE", "modsuite")
HEALTH_URL = os.environ.get("MODSUITE_URL", "http://127.0.0.1:8787") + "/healthz"

CGROUP = Path("/sys/fs/cgroup")


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return default


def _run(cmd: list, timeout: float = 5.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _cpu_usec() -> int:
    for line in _read(CGROUP / "cpu.stat").splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    return 0


def container() -> dict:
    """The box itself. cgroup figures, not /proc — inside an LXC, /proc/meminfo
    reports the *host's* memory, which makes 512 MB look like plenty."""
    before, t0 = _cpu_usec(), time.time()
    time.sleep(0.4)
    after, t1 = _cpu_usec(), time.time()
    cores = os.cpu_count() or 1
    busy = (after - before) / 1e6 / max(t1 - t0, 0.001) / cores * 100 if after else 0.0

    used = int(_read(CGROUP / "memory.current", "0") or 0)
    limit_raw = _read(CGROUP / "memory.max", "max")
    limit = int(limit_raw) if limit_raw.isdigit() else 0
    if not limit or not used:
        # No cgroup ceiling on this container. lxcfs still shows the real one
        # in /proc/meminfo, and without this the memory row vanishes entirely
        # on exactly the boxes where memory is worth watching.
        info = {}
        for line in _read(Path("/proc/meminfo")).splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.strip().split()[0]) * 1024 if rest.strip() else 0
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        if total:
            limit = total
            used = total - available

    disks, seen_devices = [], set()
    for mount in ("/", os.path.dirname(DB_PATH) or "/"):
        try:
            st = os.statvfs(mount)
            device = os.stat(mount).st_dev
        except OSError:
            continue
        if device in seen_devices:
            continue            # same filesystem, mounted somewhere else too
        seen_devices.add(device)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        disks.append({"mount": mount, "total": total, "used": total - free,
                      "percent": round((total - free) / total * 100, 1) if total else 0})

    load = _read(Path("/proc/loadavg")).split()[:3]
    return {
        "cpu_percent": round(busy, 1),
        "cores": cores,
        "load": [float(x) for x in load] if len(load) == 3 else [0, 0, 0],
        "memory_used": used,
        "memory_limit": limit,
        "memory_percent": round(used / limit * 100, 1) if limit else 0,
        "uptime": float((_read(Path("/proc/uptime")) or "0").split()[0]),
        "disks": disks,
        "hostname": _read(Path("/etc/hostname")) or "?",
    }


def service() -> dict:
    """The mod tool as systemd sees it, plus whether it actually answers."""
    props = {}
    raw = _run(["systemctl", "show", SERVICE, "-p", "ActiveState",
                "-p", "SubState", "-p", "MainPID", "-p", "MemoryCurrent",
                "-p", "ActiveEnterTimestampMonotonic", "-p", "NRestarts"])
    for line in raw.splitlines():
        key, _, value = line.partition("=")
        props[key] = value

    pid = int(props.get("MainPID", "0") or 0)
    running_for = 0.0
    if pid:
        elapsed = _run(["ps", "-o", "etimes=", "-p", str(pid)])
        if elapsed.strip().isdigit():
            running_for = float(elapsed.strip())

    health, ms, code = False, 0.0, 0
    start = time.time()
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as r:
            code = r.status
            r.read(200)
            health = code == 200
    except Exception:
        health = False
    ms = round((time.time() - start) * 1000, 1)

    mem = props.get("MemoryCurrent", "0")
    return {
        "state": props.get("ActiveState", "?"),
        "sub": props.get("SubState", "?"),
        "pid": pid,
        "memory": int(mem) if mem.isdigit() else 0,
        "restarts": int(props.get("NRestarts", "0") or 0),
        "running_for": max(running_for, 0),
        "healthy": health,
        "health_ms": ms,
        "health_code": code,
    }


def _one(conn, sql: str, args: tuple = ()) -> int:
    try:
        row = conn.execute(sql, args).fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0


def database() -> dict:
    """Counts only. Nothing here names a player or a moderator: this page has
    no sign-in, so it gets aggregates and nothing that identifies anybody."""
    now = time.time()
    out = {"path": DB_PATH, "size": 0, "wal": 0, "ok": False}
    for suffix, key in (("", "size"), ("-wal", "wal")):
        try:
            out[key] = os.path.getsize(DB_PATH + suffix)
        except OSError:
            pass
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        out["error"] = str(e)
        return out

    try:
        out["ok"] = True
        day = now - 86400
        out["incidents"] = _one(conn, "SELECT COUNT(*) FROM incidents "
                                      "WHERE COALESCE(deleted,0)=0")
        out["incidents_24h"] = _one(conn, "SELECT COUNT(*) FROM incidents "
                                          "WHERE COALESCE(deleted,0)=0 AND created_at > ?", (day,))
        out["age_checks"] = _one(conn, "SELECT COUNT(*) FROM age_checks "
                                       "WHERE COALESCE(deleted,0)=0")
        out["age_checks_24h"] = _one(conn, "SELECT COUNT(*) FROM age_checks "
                                           "WHERE COALESCE(deleted,0)=0 AND created_at > ?", (day,))
        out["sessions"] = _one(conn, "SELECT COUNT(*) FROM web_sessions "
                                     "WHERE expires_at > ?", (now,))
        out["pending_actions"] = _one(
            conn, "SELECT COUNT(*) FROM pending_actions "
                  "WHERE resolved_at IS NULL AND COALESCE(dismissed,0)=0")

        # Agents: one row per reporter, so "live" is how many are still talking.
        out["agents_live"] = _one(conn, "SELECT COUNT(*) FROM rosters "
                                        "WHERE seen_at > ?", (now - 300,))
        out["agents"] = _one(conn, "SELECT COUNT(*) FROM rosters")

        queue = {}
        try:
            for kind, n in conn.execute(
                    "SELECT kind, COUNT(*) FROM group_actions WHERE done_at IS NULL "
                    "AND cancelled_at IS NULL GROUP BY kind"):
                queue[kind] = n
        except sqlite3.Error:
            pass
        out["queue"] = queue
        out["queue_total"] = sum(queue.values())
        out["queue_done_1h"] = _one(conn, "SELECT COUNT(*) FROM group_actions "
                                          "WHERE done_at > ?", (now - 3600,))
        out["queue_done_24h"] = _one(conn, "SELECT COUNT(*) FROM group_actions "
                                           "WHERE done_at > ?", (day,))
        last = conn.execute("SELECT MAX(done_at) FROM group_actions").fetchone()
        out["queue_last_done"] = float(last[0]) if last and last[0] else 0.0

        # Bans read out of VRChat's audit log, and how fresh that read is.
        out["audit_bans"] = _one(conn, "SELECT COUNT(*) FROM incidents "
                                       "WHERE origin='vrchat-audit' AND trigger LIKE 'Ban%' "
                                       "AND COALESCE(deleted,0)=0")
        mark = conn.execute("SELECT value FROM tool_state WHERE key LIKE "
                            "'audit_ban_seen:%' LIMIT 1").fetchone()
        out["audit_watermark"] = float(mark[0]) if mark and mark[0] else 0.0

        # Every start and stop, so "it keeps crashing" can be answered by
        # looking. A start whose previous event was also a start means the
        # run before it was killed rather than asked to leave.
        try:
            events = [dict(zip(("at", "event", "pid", "detail"), row))
                      for row in conn.execute(
                          "SELECT at, event, pid, detail FROM service_log "
                          "ORDER BY at DESC LIMIT 40")]
        except sqlite3.Error:
            events = []
        out["starts_24h"] = sum(1 for e in events
                                if e["event"] == "start" and e["at"] > day)
        out["unclean_24h"] = sum(1 for e in events
                                 if e["event"] == "start" and e["at"] > day
                                 and "unclean" in (e["detail"] or ""))
        out["last_start"] = next((e["at"] for e in events
                                  if e["event"] == "start"), 0.0)
        out["events"] = events[:8]
    except sqlite3.Error as e:
        out["error"] = str(e)
    finally:
        conn.close()
    return out


def journal_errors(lines: int = 5) -> list:
    raw = _run(["journalctl", "-u", SERVICE, "--since", "-2 hours",
                "--no-pager", "-o", "cat"], timeout=10)
    bad = [l for l in raw.splitlines()
           if any(word in l.lower() for word in ("error", "traceback", "exception"))]
    return bad[-lines:]


def everything() -> dict:
    return {
        "at": time.time(),
        "container": container(),
        "service": service(),
        "database": database(),
        "errors": journal_errors(),
    }


# ---------------- formatting, shared by the shell script and the page ----------------

def human_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit in ("B", "K") else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def human_time(seconds: float) -> str:
    if seconds <= 0:
        return "never"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def as_text(data: dict) -> str:
    c, s, d = data["container"], data["service"], data["database"]
    lines = []
    add = lines.append

    add(f"  {c['hostname']}  up {human_time(c['uptime'])}"
        f"   load {c['load'][0]:.2f} {c['load'][1]:.2f} {c['load'][2]:.2f}")
    add(f"  CPU   {c['cpu_percent']:5.1f}%  of {c['cores']} cores")
    if c["memory_limit"]:
        add(f"  RAM   {c['memory_percent']:5.1f}%  "
            f"{human_bytes(c['memory_used'])} of {human_bytes(c['memory_limit'])}")
    for disk in c["disks"]:
        add(f"  Disk  {disk['percent']:5.1f}%  "
            f"{human_bytes(disk['used'])} of {human_bytes(disk['total'])}  {disk['mount']}")

    add("")
    mark = "OK " if (s["state"] == "active" and s["healthy"]) else "!! "
    add(f"  {mark}mod tool: {s['state']}/{s['sub']}"
        f"   up {human_time(s['running_for'])}"
        f"   {human_bytes(s['memory'])}"
        f"   pid {s['pid']}")
    add(f"      health {'answers in ' + str(s['health_ms']) + 'ms' if s['healthy'] else 'NOT ANSWERING'}"
        f"   restarts {s['restarts']}")

    add("")
    if not d.get("ok"):
        add(f"  !! database unreadable: {d.get('error', 'unknown')}")
    else:
        add(f"  Database  {human_bytes(d['size'])}"
            f"  (+{human_bytes(d['wal'])} wal)")
        add(f"  Incidents {d['incidents']:>7}   {d['incidents_24h']} today")
        add(f"  Age check {d['age_checks']:>7}   {d['age_checks_24h']} today")
        add(f"  Agents    {d['agents_live']:>7} live of {d['agents']}")
        add(f"  Sessions  {d['sessions']:>7} signed in")
        add(f"  Prompts   {d['pending_actions']:>7} awaiting a reason")
        queue = ", ".join(f"{n} {kind}" for kind, n in sorted(d["queue"].items())) or "empty"
        add(f"  Queue     {d['queue_total']:>7} waiting ({queue})")
        add(f"            {d['queue_done_1h']} sent in the last hour,"
            f" {d['queue_done_24h']} today,"
            f" last {human_time(data['at'] - d['queue_last_done'])} ago")
        add(f"  Audit     {d['audit_bans']:>7} bans read from VRChat,"
            f" watermark {human_time(data['at'] - d['audit_watermark'])} old")
        add(f"  Restarts  {d.get('starts_24h', 0):>7} in 24h"
            f"{', ' + str(d['unclean_24h']) + ' after an unclean stop'
              if d.get('unclean_24h') else ''}")

    if data["errors"]:
        add("")
        add("  Recent errors in the service log:")
        for line in data["errors"]:
            add(f"    {line[:110]}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    data = everything()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        print(as_text(data))
