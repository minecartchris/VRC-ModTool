"""Roster agent — reports who is in your VRChat instance to the mod server.

VRChat's API will tell you an instance holds 48 people but never who they are;
the names exist only in the output log of a client that is actually in the
instance. So when the server is hosted somewhere else, someone in the world has
to report the roster, and this is the smallest thing that can do it.

It is not the desktop app: no Vosk model, no audio capture, no Tkinter, no
clipping. It tails the VRChat log and POSTs the roster. One moderator running
this gives every browser a live Screening page.

    python agent.py --server https://mods.example.com

On first run it prints a link. A moderator opens that link in a browser where
they are already signed in, and this PC is handed a key that can do one thing:
report a roster. The key travels server-to-agent, so nobody has to read it off
a screen or paste it into a chat. Settings are remembered in agent_config.json,
so after that:

    python agent.py

`--pair` sets the PC up again — after a key is revoked, say. A key can also be
pasted at the first-run prompt, or given as `--token`, for a machine with no
browser.

Needs only `requests` (pip install requests) plus Python 3.10+.
"""

import argparse
import json
import socket
import sys
import time
import uuid
from pathlib import Path

import requests

import vrc_log

# VRChat world names are full of emoji and the Windows console this runs in is
# usually cp1252, where printing one raises UnicodeEncodeError and kills the
# agent mid-report. Degrade to "?" instead. Our own strings stay ASCII so they
# read correctly whatever the console is.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError):       # not a real console, e.g. piped
        pass


def _base_dir() -> Path:
    """Where to keep agent_config.json.

    Next to the .exe when frozen — PyInstaller unpacks the bundle to a temp
    directory that is deleted on exit, so writing there would silently lose
    the settings on every run.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = _base_dir() / "agent_config.json"
DESKTOP_CONFIG = _base_dir() / "config.json"

# Written by build_agent.py when packaging the .exe, so a moderator can just
# run it. The token baked in here is the roster-only one — it cannot read
# incidents or age checks. Absent in a normal source checkout.
try:
    from agent_baked import SERVER as BAKED_SERVER, TOKEN as BAKED_TOKEN
except ImportError:
    BAKED_SERVER = BAKED_TOKEN = ""

#: Prove we're alive this often even when nobody joins or leaves. Must stay
#: well under the server's 180s staleness cutoff.
HEARTBEAT = 30.0
#: How often to look for a change worth reporting immediately.
POLL = 3.0
#: How often to ask the server whether the pairing link has been opened yet.
PAIR_POLL = 3.0


def pair(session: requests.Session, server: str, name: str) -> str:
    """Trade a link for a key, without the human ever handling the key.

    We ask the server for a code, print the link, and wait. A moderator opens
    it in a browser where they are already signed in, and the key comes back
    down this connection — so it is never on screen, in a screenshot or in a
    chat message. The secret below is what stops somebody who merely sees the
    code from collecting the key instead of us.
    """
    try:
        r = session.post(f"{server}/api/agent/pair/start",
                         json={"client_name": name}, timeout=20)
        r.raise_for_status()
        start = r.json()
    except requests.RequestException as e:
        raise SystemExit(f"Couldn't reach {server}: {e}")

    print("\n  Open this link while signed in to the mod panel:\n")
    print(f"      {start['url']}\n")
    print(f"  (code {start['code']}, good for "
          f"{int(start.get('expires_in', 600)) // 60} minutes)")
    try:
        import webbrowser
        webbrowser.open(start["url"])
        print("  Tried to open it in your browser.")
    except Exception:
        pass
    print("\n  Waiting for approval... Ctrl+C to give up.")

    deadline = time.time() + float(start.get("expires_in", 600))
    while time.time() < deadline:
        time.sleep(PAIR_POLL)
        try:
            r = session.post(f"{server}/api/agent/pair/poll",
                             json={"code": start["code"],
                                   "secret": start["secret"]}, timeout=20)
        except requests.RequestException:
            continue                      # keep waiting through a blip
        if r.status_code == 410:
            raise SystemExit(f"  {r.json().get('error', 'that code is done')}")
        if not r.ok:
            raise SystemExit(f"  pairing failed ({r.status_code})")
        data = r.json()
        if data.get("status") == "approved":
            print(f"\n  Approved by {data.get('name') or 'a moderator'}. "
                  "This PC now reports as them.")
            return data["token"]
    raise SystemExit("  That code expired. Run this again for a new one.")


def first_run(cfg: dict, *, force_pair: bool = False) -> dict:
    """Ask for whatever is still missing, then pair or take a pasted key."""
    if not cfg["server"]:
        entered = input("Mod panel address (e.g. https://mods.example.com): ")
        cfg["server"] = entered.strip().rstrip("/")
        if not cfg["server"]:
            raise SystemExit("Need the server address to do anything.")

    if not force_pair:
        print(f"\nFirst run - this PC is not set up for {cfg['server']} yet.")
        print("  [Enter]  set it up in your browser (recommended)")
        print("  or paste a key from Settings -> Your agents")
        answer = input("\n> ").strip()
        if answer:
            cfg["token"] = answer
            return cfg
    cfg["token"] = pair(requests.Session(), cfg["server"], cfg["name"])
    return cfg


def load_settings(args) -> dict:
    """CLI beats agent_config.json, which beats the built-in defaults."""
    cfg = {"server": BAKED_SERVER, "token": BAKED_TOKEN,
           "name": "", "client_id": ""}
    # A packaged agent ignores the desktop app's config: it is aimed at the
    # hosted server it was built for, not whatever a dev machine points at.
    sources = [(CONFIG_PATH, ("server", "token", "name", "client_id"))]
    if not BAKED_SERVER:
        sources.insert(0, (DESKTOP_CONFIG, ("sync_url", "sync_token",
                                            "sync_client_name",
                                            "sync_client_id")))
    for path, keys in sources:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        for field, key in zip(("server", "token", "name", "client_id"), keys):
            if raw.get(key):
                cfg[field] = raw[key]

    for field in ("server", "token", "name"):
        if getattr(args, field, None):
            cfg[field] = getattr(args, field)
    cfg["server"] = cfg["server"].rstrip("/")
    if not cfg["name"]:
        cfg["name"] = socket.gethostname()
    if not cfg["client_id"]:
        cfg["client_id"] = uuid.uuid4().hex[:12]
    return cfg


def save_settings(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"  (couldn't save {CONFIG_PATH.name}: {e})")


def post_roster(session: requests.Session, cfg: dict, snap: dict) -> str:
    """Send one roster. Returns the server's reason for discarding it, if any."""
    r = session.post(
        f"{cfg['server']}/api/sync/roster",
        json={"client_id": cfg["client_id"], "client_name": cfg["name"],
              "roster": {"world_name": snap["world_name"],
                         "world_id": snap["world_id"],
                         "instance_id": snap["instance_id"],
                         "players": snap["players"]}},
        headers={"X-Sync-Token": cfg["token"]}, timeout=20)
    if r.status_code == 401:
        raise SystemExit("The server rejected this PC's key - it was most "
                         "likely revoked in the panel.\nRun this again with "
                         "--pair to set it up afresh.")
    if r.status_code == 503:
        raise SystemExit("Server has the sync API disabled (no sync_token set).")
    r.raise_for_status()
    try:
        return r.json().get("ignored") or ""
    except ValueError:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Report your VRChat instance "
                                             "roster to the mod server.")
    ap.add_argument("--server", help="e.g. https://mods.example.com")
    ap.add_argument("--token", help="sync_token from the server config")
    ap.add_argument("--name", help="how this PC appears in the web UI")
    ap.add_argument("--once", action="store_true",
                    help="send one roster and exit")
    ap.add_argument("--pair", action="store_true",
                    help="set this PC up again, even if it already has a key")
    args = ap.parse_args()

    cfg = load_settings(args)
    if args.pair or not cfg["token"] or not cfg["server"]:
        cfg = first_run(cfg, force_pair=args.pair)
    save_settings(cfg)

    if not vrc_log.LOG_DIR.exists():
        raise SystemExit(f"No VRChat log directory at {vrc_log.LOG_DIR}.\n"
                         "Run this on the PC that plays VRChat.")

    print(f"Roster agent -> {cfg['server']}  (as {cfg['name']})")
    print("Reading VRChat's log. Leave this running while you're in the "
          "instance; Ctrl+C to stop.")

    watcher = vrc_log.VRCLogWatcher()
    watcher.start()

    session = requests.Session()
    last_revision = -1
    last_sent = 0.0
    complained = False
    waiting = True
    said_ignored = ""          # instance we have already explained away
    # --once still has to wait for the watcher to parse the log; exiting on the
    # first empty snapshot would send nothing at all.
    deadline = time.time() + 30 if args.once else None

    try:
        while True:
            snap = watcher.snapshot()
            due = (snap["revision"] != last_revision
                   or time.time() - last_sent >= HEARTBEAT)
            # Never report an empty snapshot as if it were an instance; it
            # would overwrite a real roster from another reporter.
            if due and (snap["players"] or snap["world_id"]):
                try:
                    ignored = post_roster(session, cfg, snap)
                    if ignored and snap["instance_id"] != said_ignored:
                        # Said once per instance, not every heartbeat: this is
                        # normal when a moderator steps into a private world.
                        said_ignored = snap["instance_id"]
                        print(f"  [{time.strftime('%H:%M:%S')}] "
                              f"{snap['world_name'] or 'this world'} - "
                              f"{ignored}")
                    elif not ignored and (snap["revision"] != last_revision
                                          or complained):
                        said_ignored = ""
                        print(f"  [{time.strftime('%H:%M:%S')}] "
                              f"{snap['world_name'] or 'unknown world'} - "
                              f"{len(snap['players'])} players")
                    last_revision, last_sent, complained = (
                        snap["revision"], time.time(), False)
                    waiting = False
                    if args.once:
                        return 0
                except requests.RequestException as e:
                    if not complained:      # don't spam while it's down
                        print(f"  can't reach the server ({e}); retrying")
                        complained = True
            elif waiting and deadline is None and time.time() - last_sent > 20:
                last_sent = time.time()     # reuse as a "said this" marker
                print("  waiting for VRChat to join a world...")
            if deadline and time.time() > deadline:
                print("  gave up waiting for an instance (is VRChat in a "
                      "world?)")
                return 1
            time.sleep(POLL)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    finally:
        watcher.stop()


if __name__ == "__main__":
    sys.exit(main())
