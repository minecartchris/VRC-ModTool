#!/usr/bin/env python3
"""A terminal dashboard for the mod suite, with the buttons attached.

    ./tui.py                 inside the container
    ./tui.py --ct 101        from the Proxmox host, driving the container

Run from the host it can also start a container that is stopped, which is the
one thing it cannot do from the inside. Everything else works either way.

Stats come from collect.py, the same one the status page uses, gathered on a
background thread so a slow health check never freezes the keyboard.
"""
import argparse
import curses
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import collect                                            # noqa: E402

REFRESH = 5.0
CONTAINER_PATH = "/opt/modsuite/app/ops"


class Runner:
    """Runs commands either here, or inside the container from the host."""

    def __init__(self, ct: str = ""):
        self.ct = ct

    @property
    def remote(self) -> bool:
        return bool(self.ct)

    def inside(self, cmd: list, timeout: float = 60.0) -> tuple:
        """Run a command in the container, wherever we happen to be."""
        full = (["pct", "exec", self.ct, "--"] + cmd) if self.remote else cmd
        return self._run(full, timeout)

    def host(self, cmd: list, timeout: float = 120.0) -> tuple:
        """Run a pct command. Only meaningful from the Proxmox host."""
        if not self.remote:
            return 1, "", "not running on the Proxmox host"
        return self._run(cmd, timeout)

    @staticmethod
    def _run(cmd: list, timeout: float) -> tuple:
        try:
            done = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
            return done.returncode, done.stdout.strip(), done.stderr.strip()
        except subprocess.TimeoutExpired:
            return 1, "", f"timed out after {timeout:.0f}s"
        except OSError as e:
            return 1, "", str(e)

    def stats(self) -> dict:
        if not self.remote:
            return collect.everything()
        code, out, err = self.inside(
            ["python3", f"{CONTAINER_PATH}/collect.py", "--json"], timeout=30)
        if code != 0 or not out:
            return {"error": err or "container did not answer", "at": time.time()}
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            return {"error": f"unreadable stats: {e}", "at": time.time()}


class Poller(threading.Thread):
    """Collects in the background so the keyboard never waits on a health check."""

    def __init__(self, runner: Runner):
        super().__init__(daemon=True)
        self.runner = runner
        self.data: dict = {}
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.busy = False

    def run(self):
        while not self.stop.is_set():
            self.busy = True
            try:
                self.data = self.runner.stats()
            except Exception as e:                # a dashboard must not die
                self.data = {"error": f"{type(e).__name__}: {e}", "at": time.time()}
            self.busy = False
            self.wake.wait(REFRESH)
            self.wake.clear()


ACTIONS = [
    # key, label, needs confirming, what it does
    ("s", "start tool", False, ("service", "start")),
    ("t", "stop tool", True, ("service", "stop")),
    ("r", "restart tool", False, ("service", "restart")),
    ("m", "restart status page", False, ("monitor", "restart")),
    ("B", "reboot container", True, ("container", "reboot")),
    ("H", "halt container", True, ("container", "stop")),
    ("U", "start container", False, ("container", "start")),
]


class Dashboard:
    def __init__(self, screen, runner: Runner, poller: Poller):
        self.screen = screen
        self.runner = runner
        self.poller = poller
        self.message = ""
        self.message_tone = 0
        self.confirm: tuple | None = None

    # ---------------- doing things ----------------
    def act(self, target: str, verb: str) -> None:
        self.say(f"{verb}ing {target}...", 0)
        self.draw()
        self.screen.refresh()

        if target == "container":
            if not self.runner.remote:
                if verb in ("reboot", "stop"):
                    # Inside the container, systemd can do it — but nothing in
                    # here can undo it, which is worth saying out loud.
                    code, out, err = self.runner.inside(
                        ["systemctl", "reboot" if verb == "reboot" else "poweroff"])
                else:
                    code, out, err = 1, "", (
                        "a container cannot start itself - run this from the "
                        "Proxmox host with --ct")
            else:
                code, out, err = self.runner.host(["pct", verb, self.runner.ct])
        else:
            unit = "modsuite" if target == "service" else "modmonitor"
            code, out, err = self.runner.inside(["systemctl", verb, unit])

        if code == 0:
            self.say(f"{target} {verb} done.", 1)
        else:
            self.say(f"{target} {verb} failed: {(err or out or '?')[:70]}", 2)
        self.poller.wake.set()

    def say(self, text: str, tone: int) -> None:
        self.message, self.message_tone = text, tone

    # ---------------- drawing ----------------
    def colour(self, percent: float) -> int:
        return 1 if percent < 70 else (3 if percent < 90 else 2)

    def bar(self, y: int, x: int, width: int, percent: float) -> None:
        percent = max(0.0, min(100.0, percent))
        filled = int(width * percent / 100)
        try:
            self.screen.addstr(y, x, "[")
            self.screen.addstr(y, x + 1, "#" * filled,
                               curses.color_pair(self.colour(percent)))
            self.screen.addstr(y, x + 1 + filled, "." * (width - filled))
            self.screen.addstr(y, x + 1 + width, "]")
        except curses.error:
            pass

    def line(self, y: int, text: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if 0 <= y < height:
            try:
                self.screen.addstr(y, 0, text[:width - 1], attr)
            except curses.error:
                pass

    def draw(self) -> None:
        self.screen.erase()
        data = self.poller.data
        height, width = self.screen.getmaxyx()
        hb, ht = collect.human_bytes, collect.human_time

        where = f"container {self.runner.ct}" if self.runner.remote else "this container"
        head = f" Mod Suite - {where} - {time.strftime('%H:%M:%S')}"
        self.line(0, head.ljust(width - 1), curses.A_REVERSE)

        if not data:
            self.line(2, "  collecting...")
            return
        if data.get("error"):
            self.line(2, f"  cannot read the container: {data['error']}",
                      curses.color_pair(2))
            self.footer(height)
            return

        c, s, d = data["container"], data["service"], data["database"]
        y = 2

        self.line(y, f"  CPU   {c['cpu_percent']:5.1f}%  of {c['cores']} cores")
        self.bar(y, 24, 28, c["cpu_percent"])
        y += 1
        self.line(y, f"  RAM   {c['memory_percent']:5.1f}%  "
                     f"{hb(c['memory_used'])} of {hb(c['memory_limit'])}")
        self.bar(y, 24, 28, c["memory_percent"])
        y += 1
        for disk in c["disks"]:
            self.line(y, f"  Disk  {disk['percent']:5.1f}%  "
                         f"{hb(disk['used'])} of {hb(disk['total'])}")
            self.bar(y, 24, 28, disk["percent"])
            y += 1
        self.line(y, f"  Up    {ht(c['uptime'])}   load "
                     f"{c['load'][0]:.2f} {c['load'][1]:.2f} {c['load'][2]:.2f}")
        y += 2

        good = s["state"] == "active" and s["healthy"]
        self.line(y, f"  MOD TOOL  {s['state']}/{s['sub']}",
                  curses.color_pair(1 if good else 2) | curses.A_BOLD)
        y += 1
        health = (f"answers in {s['health_ms']:.0f}ms" if s["healthy"]
                  else "NOT ANSWERING")
        self.line(y, f"    health {health}   up {ht(s['running_for'])}   "
                     f"{hb(s['memory'])}   pid {s['pid']}   "
                     f"{s['restarts']} restarts",
                  curses.color_pair(0 if s["healthy"] else 2))
        y += 2

        if d.get("ok"):
            queue_bits = ", ".join(f"{n} {k}" for k, n in sorted(d["queue"].items())) \
                or "empty"
            stalled = d["queue_total"] > 0 and \
                (data["at"] - d["queue_last_done"]) > 3600
            rows = [
                f"  Incidents {d['incidents']:>7}   {d['incidents_24h']} today",
                f"  Age check {d['age_checks']:>7}   {d['age_checks_24h']} today",
                f"  Agents    {d['agents_live']:>7} live    "
                f"Sessions {d['sessions']}   Prompts {d['pending_actions']}",
                f"  Queue     {d['queue_total']:>7} waiting ({queue_bits})",
                f"  Sent      {d['queue_done_1h']:>7} last hour, "
                f"{d['queue_done_24h']} today, last {ht(data['at'] - d['queue_last_done'])} ago",
                f"  Audit     {d['audit_bans']:>7} bans read, watermark "
                f"{ht(data['at'] - d['audit_watermark'])} old",
                f"  Database  {hb(d['size']):>7}   +{hb(d['wal'])} wal",
            ]
            for i, row in enumerate(rows):
                tone = curses.color_pair(2) if (stalled and i == 4) else 0
                self.line(y, row, tone)
                y += 1
        else:
            self.line(y, f"  database unreadable: {d.get('error', '?')}",
                      curses.color_pair(2))
            y += 1

        if data.get("errors"):
            y += 1
            self.line(y, "  Recent errors:", curses.A_BOLD)
            y += 1
            for err in data["errors"][-3:]:
                self.line(y, f"    {err[:width - 6]}", curses.color_pair(2))
                y += 1

        self.footer(height)

    def footer(self, height: int) -> None:
        width = self.screen.getmaxyx()[1]
        if self.confirm:
            _, label, _, _ = self.confirm
            self.line(height - 3, f"  {label}? press y to confirm, anything else to cancel"
                      .ljust(width - 1), curses.color_pair(3) | curses.A_BOLD)
        elif self.message:
            tone = [0, curses.color_pair(1), curses.color_pair(2)][self.message_tone]
            self.line(height - 3, f"  {self.message}"[:width - 1], tone)

        keys = "  ".join(f"{key} {label}" for key, label, _, _ in ACTIONS)
        self.line(height - 2, f"  {keys}"[:width - 1], curses.A_DIM)
        self.line(height - 1, "  q quit   space refresh"
                              f"   auto every {REFRESH:.0f}s"
                              f"{'   [collecting]' if self.poller.busy else ''}",
                  curses.A_DIM)

    # ---------------- keys ----------------
    def key(self, ch: int) -> bool:
        """False to quit."""
        if ch in (-1, curses.KEY_RESIZE):
            return True
        pressed = chr(ch) if 0 <= ch < 256 else ""

        if self.confirm:
            action = self.confirm
            self.confirm = None
            if pressed == "y":
                self.act(*action[3])
            else:
                self.say("cancelled.", 0)
            return True

        if pressed in ("q", "Q"):
            return False
        if pressed == " ":
            self.poller.wake.set()
            self.say("refreshing...", 0)
            return True

        for key, label, needs_confirm, what in ACTIONS:
            if pressed == key:
                if needs_confirm:
                    self.confirm = (key, label, needs_confirm, what)
                else:
                    self.act(*what)
                return True
        return True


def main(screen, runner: Runner, poller: Poller) -> None:
    curses.curs_set(0)
    screen.nodelay(True)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)

    board = Dashboard(screen, runner, poller)
    while True:
        board.draw()
        screen.refresh()
        start = time.time()
        while time.time() - start < 0.5:
            ch = screen.getch()
            if ch != -1:
                if not board.key(ch):
                    return
                break
            time.sleep(0.05)


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ct", default="",
                        help="container id, when running on the Proxmox host")
    args = parser.parse_args()

    if args.ct and not shutil.which("pct"):
        print("  --ct only works on the Proxmox host, where pct lives.")
        raise SystemExit(1)
    if not args.ct and shutil.which("pct"):
        print("  This looks like the Proxmox host. Pass --ct <id> so it knows\n"
              "  which container to watch, e.g. --ct 101.")
        raise SystemExit(1)
    if not os.environ.get("TERM"):
        os.environ["TERM"] = "xterm"

    runner = Runner(args.ct)
    poller = Poller(runner)
    poller.start()
    try:
        curses.wrapper(main, runner, poller)
    finally:
        poller.stop.set()
        poller.wake.set()


if __name__ == "__main__":
    cli()
