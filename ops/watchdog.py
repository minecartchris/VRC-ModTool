#!/usr/bin/env python3
"""Watch the mod suite from outside the container, and record every outage.

Nothing inside a container can witness its own reboot: the process dies, the
journal stops, and the next boot can only say "the last run never said
goodbye". This runs on the Proxmox host instead, so it is still awake for the
part nobody else sees.

    python3 watchdog.py --ct 101 --host 192.168.1.50

Every transition is written to /var/log/modsuite-watchdog.log as one JSON
object per line, and to the journal. When something comes back it asks
Proxmox what happened to the container while it was gone, so an outage says
"vzreboot by root@pam" rather than leaving you to go and look.

Two ports, and the difference is the diagnosis:

    8787 down, 8090 up      the mod tool died; the container is fine
    both down               the container went away
"""
import argparse
import json
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

LOG = Path("/var/log/modsuite-watchdog.log")


def reachable(url: str, timeout: float = 6.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def container_state(ct: str) -> str:
    """Ask the API, not `pct status`.

    `pct` takes the container config lock, and during a backup or a reboot -
    exactly when this is called - that lock is held and the command times out,
    so the one field that says whether the box is even there came back
    "unknown" precisely when it mattered.
    """
    try:
        raw = subprocess.run(
            ["pvesh", "get", f"/nodes/localhost/lxc/{ct}/status/current",
             "--output-format", "json"],
            capture_output=True, text=True, timeout=20)
        if raw.returncode != 0:
            return f"unknown ({(raw.stderr or '').strip()[:60]})"
        return json.loads(raw.stdout or "{}").get("status", "unknown")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return f"unknown ({type(e).__name__})"


def recent_tasks(ct: str, since: float) -> list:
    """What Proxmox did to this container during the outage.

    This is the whole point of watching from the host: an outage that
    coincides with a vzreboot is somebody pressing a button, and one that
    coincides with nothing is a real crash worth chasing.
    """
    try:
        raw = subprocess.run(
            ["pvesh", "get", "/nodes/localhost/tasks", "--vmid", ct,
             "--limit", "20", "--output-format", "json"],
            capture_output=True, text=True, timeout=30).stdout
        rows = json.loads(raw or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    out = []
    for t in rows:
        if t.get("starttime", 0) < since - 120:
            continue
        if t.get("type") in ("vncproxy", "termproxy", "push_file"):
            continue
        out.append({"type": t.get("type"), "user": t.get("user"),
                    "at": t.get("starttime"), "status": t.get("status")})
    return out


def note(event: dict) -> None:
    event["when"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = json.dumps(event)
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass                    # the journal still has it


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ct", default="101")
    ap.add_argument("--host", default="192.168.1.50")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--tool-port", type=int, default=8787)
    ap.add_argument("--monitor-port", type=int, default=8090)
    args = ap.parse_args()

    tool = f"http://{args.host}:{args.tool_port}/healthz"
    monitor = f"http://{args.host}:{args.monitor_port}/healthz"

    up = None                   # unknown until the first look
    since = time.time()
    note({"event": "watching", "ct": args.ct, "tool": tool})

    while True:
        tool_ok = reachable(tool)
        monitor_ok = reachable(monitor) if not tool_ok else True
        now = time.time()

        if tool_ok and up is not True:
            if up is False:
                gone = now - since
                note({"event": "back", "down_for_seconds": round(gone),
                      "down_for": f"{int(gone // 60)}m{int(gone % 60):02d}s",
                      "proxmox_did": recent_tasks(args.ct, since)})
            else:
                note({"event": "up"})
            up, since = True, now

        elif not tool_ok and up is not False:
            # Which of the two failures is it? The answer changes who to ask.
            state = container_state(args.ct)
            note({"event": "down",
                  "container": state,
                  "status_page": "up" if monitor_ok else "down",
                  "reading": ("the mod tool died; the container is fine"
                              if monitor_ok else
                              "the whole container is unreachable")})
            up, since = False, now

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
