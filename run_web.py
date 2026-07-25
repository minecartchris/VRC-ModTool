"""Start the web server.

    python run_web.py                 # host/port from web_config.json
    python run_web.py --port 9000     # override
    python run_web.py --init          # write a starter web_config.json

Serves the same modtool.db the desktop app uses. See README-web.md.
"""

import argparse
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn

from paths import HERE

# Windows consoles still default to cp1252, which raises on any non-ASCII we
# (or uvicorn) print. Degrade instead of crashing on startup.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from webapp import config as webconfig
from webapp.server import create_app


def init_config() -> None:
    if webconfig.CONFIG_PATH.exists():
        print(f"{webconfig.CONFIG_PATH.name} already exists — leaving it alone.")
        return
    cfg = dict(webconfig.DEFAULTS)
    cfg["sync_token"] = secrets.token_urlsafe(32)
    webconfig.save(cfg)
    print(f"Wrote {webconfig.CONFIG_PATH}")
    print("Now set:")
    print("  staff_group  — your moderator group's ID, short code, or name")
    print("  vrc_contact  — a real email/Discord handle for the VRChat API")
    print(f"  sync_token   — generated for you: {cfg['sync_token']}")
    print("                 paste it into the desktop app's Settings tab")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--init", action="store_true",
                    help="write a starter web_config.json and exit")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--reload", dest="reload", action="store_true",
                    default=None, help="restart on code changes")
    ap.add_argument("--no-reload", dest="reload", action="store_false",
                    help="don't watch for code changes")
    ap.add_argument("--supervisor-pid", type=int,
                    help=argparse.SUPPRESS)   # set by supervise()
    args = ap.parse_args()

    if args.supervisor_pid:
        _exit_with_parent(args.supervisor_pid)

    if args.init:
        init_config()
        return

    cfg = webconfig.load()
    host = args.host or cfg["host"]
    port = args.port or int(cfg["port"])

    if not cfg.get("staff_group"):
        print("WARNING: no staff_group configured — nobody can sign in.\n"
              "         Run `python run_web.py --init`, then edit "
              f"{webconfig.CONFIG_PATH.name}.", file=sys.stderr)
    if host not in ("127.0.0.1", "localhost") and not cfg.get("https_only"):
        print("WARNING: listening beyond localhost without https_only set.\n"
              "         Put this behind a TLS reverse proxy — sign-in sends "
              "VRChat credentials.", file=sys.stderr)

    reload = cfg.get("auto_reload", True) if args.reload is None else args.reload

    if reload:
        supervise(host, port)
    else:
        print(f"Mod Suite web on http://{host}:{port}", flush=True)
        uvicorn.run(create_app(cfg), host=host, port=port)


# Files worth restarting for, and trees that must never be walked: the Vosk
# models are gigabytes and modtool.db changes on every write, which would put
# the server in a restart loop.
_WATCH_EXTS = (".py", ".html", ".css", ".js")
_SKIP_DIRS = {".venv", ".git", "__pycache__", "incident_shots", "node_modules"}


def _worth_reloading(_change, path: str) -> bool:
    p = Path(path)
    if any(part in _SKIP_DIRS or part.startswith("vosk-model-")
           for part in p.parts):
        return False
    return p.suffix in _WATCH_EXTS


def supervise(host: str, port: int) -> None:
    """Run the server as a child process and restart it when code changes.

    Not uvicorn's --reload: on Windows its worker restart hangs, leaving the
    *old* process serving forever, so edits appear to be picked up ("Reloading
    ...") while the running code never actually changes — the worst kind of
    failure. Replacing the whole process is slower by a second but can't
    half-succeed. Signed-in moderators are unaffected either way, since
    sessions live in the database rather than in memory.
    """
    from watchfiles import watch

    def spawn() -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--no-reload",
             "--host", host, "--port", str(port),
             "--supervisor-pid", str(os.getpid())])

    child = spawn()
    print(f"Mod Suite web on http://{host}:{port}"
          f"  (restarting on code changes; Ctrl+C to stop)", flush=True)
    try:
        for changes in watch(str(HERE), watch_filter=_worth_reloading,
                             debounce=500):
            changed = ", ".join(sorted({Path(p).name for _, p in changes}))
            print(f"\n[reload] {changed}", flush=True)
            _stop(child)
            time.sleep(0.4)          # let the listening socket be released
            child = spawn()
    except KeyboardInterrupt:
        pass
    finally:
        _stop(child)


def _process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    # Deliberately not os.kill(pid, 0): on Windows that calls TerminateProcess
    # and would kill the supervisor instead of probing it.
    import ctypes
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
    k32.CloseHandle(handle)
    return bool(ok) and code.value == 259          # STILL_ACTIVE


def _exit_with_parent(pid: int) -> None:
    """Shut down if the supervisor dies without cleaning up.

    Killing the supervisor with anything but Ctrl+C skips its finally block —
    on Windows TerminateProcess is immediate — which would otherwise strand
    this child holding the port, still serving stale code.
    """
    def watch_parent() -> None:
        while _process_alive(pid):
            time.sleep(2)
        os._exit(0)

    threading.Thread(target=watch_parent, daemon=True).start()


def _stop(child: subprocess.Popen) -> None:
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


if __name__ == "__main__":
    main()
