"""Run VRChat as quietly as this machine allows, with the roster agent.

**VRChat has no headless mode.** It is a Unity client with anti-cheat and no
dedicated server build; Unity's own `-batchmode` / `-nographics` are documented
as unsupported. And it could not work anyway: the roster exists *because* a
real client is in the room rendering it. The names come out of that client's
log and nowhere else — VRChat's API will tell you an instance holds 48 people
and refuse to say who.

So this gets as close as the client allows, using VRChat's own documented
launch options:

    (started through VRChat's own launch.exe — running VRChat.exe directly
    puts the client in offline testing mode, where it cannot travel to any
    online world and so has no instance to report)

    --no-vr                 desktop mode, so SteamVR never starts
    --fps=5                 the real lever; the frame cap is what costs GPU
    --process-priority=-2   idle, so it yields to whatever you are doing
    --main-thread-priority=-2
    --affinity=3            two CPU threads, not all of them
    --profile=1             a separate login, so your own VRChat is untouched

plus a minimised window. On a machine that would otherwise render 90fps of a
40-player instance, five frames a second at idle priority is the difference
between a busy GPU and a background task.

    python roster_host.py                     # launch both, supervise
    python roster_host.py --fps 1 --profile 2
    python roster_host.py --no-launch         # VRChat already running

Ctrl+C stops both. If VRChat exits, the agent is stopped with it rather than
left reporting a room nobody is in.

Set the graphics quality low *once*, in the client, on the profile this uses:
the launch options cannot reach those settings, and they persist per profile.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import vrc_log

HERE = Path(__file__).resolve().parent

#: Frames per second for a client nobody is looking at. Not zero: VRChat needs
#: to keep simulating to stay in the instance and keep writing the log.
QUIET_FPS = 5

#: Two threads. Enough for the client to tick, few enough to stay out of the
#: way of a game on the same machine.
QUIET_AFFINITY = "3"


def find_vrchat() -> Path | None:
    """Locate VRChat's launcher via Steam, or None if we cannot.

    `launch.exe`, not `VRChat.exe`. Starting the game binary directly drops
    the client into offline testing mode — it comes up, it just refuses to
    travel to online worlds, so there is no instance and no roster. The
    launcher is what sets up a normal online session, and it forwards our
    arguments through to the game.
    """
    candidates = []
    try:
        import winreg
        for root, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(root, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            candidates.append(Path(winreg.QueryValueEx(k, name)[0]))
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    candidates += [Path(r"C:\Program Files (x86)\Steam"),
                   Path(r"C:\Program Files\Steam")]

    libraries = []
    for steam in candidates:
        libraries.append(steam)
        # Steam spreads games across drives; the library list is a VDF, but the
        # only part we need is the quoted paths.
        vdf = steam / "steamapps" / "libraryfolders.vdf"
        try:
            for line in vdf.read_text(encoding="utf-8", errors="ignore").splitlines():
                if '"path"' in line.lower():
                    parts = line.split('"')
                    if len(parts) >= 4:
                        libraries.append(Path(parts[3].replace("\\\\", "\\")))
        except OSError:
            pass

    for lib in libraries:
        folder = lib / "steamapps" / "common" / "VRChat"
        for name in ("launch.exe", "VRChat.exe"):
            exe = folder / name
            if exe.is_file():
                return exe
    return None


def game_running() -> bool:
    """Whether VRChat itself is up.

    Needed because launch.exe hands off to VRChat.exe and exits within
    seconds: waiting on the launcher's own process would read as "VRChat
    closed" the moment it started.
    """
    try:
        out = subprocess.run(["tasklist", "/fi", "imagename eq VRChat.exe",
                              "/nh"], capture_output=True, text=True,
                             timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return True          # cannot tell; assume it is fine rather than quit
    return "VRChat.exe" in out


def launch_vrchat(exe: Path, args) -> subprocess.Popen:
    cmd = [str(exe), "--no-vr", f"--fps={args.fps}",
           f"--profile={args.profile}",
           f"--process-priority={args.priority}",
           f"--main-thread-priority={args.priority}"]
    if args.affinity:
        cmd.append(f"--affinity={args.affinity}")

    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 7          # SW_SHOWMINNOACTIVE
    print("  " + " ".join(cmd[1:]))
    return subprocess.Popen(cmd, cwd=str(exe.parent), startupinfo=startupinfo)


def newest_log_time() -> float:
    """When VRChat last wrote a log, or 0. How we know a client is alive."""
    try:
        logs = sorted(vrc_log.LOG_DIR.glob("output_log_*.txt"),
                      key=lambda p: p.stat().st_mtime)
        return logs[-1].stat().st_mtime if logs else 0.0
    except OSError:
        return 0.0


def wait_for_client(deadline: float) -> bool:
    """Wait until VRChat is writing a log, so the agent has something to read."""
    start = newest_log_time()
    print("  waiting for VRChat to start writing its log...")
    while time.time() < deadline:
        now = newest_log_time()
        if now > start or (now and time.time() - now < 30):
            return True
        time.sleep(2)
    return False


def start_agent(args) -> subprocess.Popen:
    exe = HERE / "dist" / "VRChatRosterAgent.exe"
    if exe.is_file() and not args.from_source:
        cmd = [str(exe)]
    else:
        cmd = [sys.executable, str(HERE / "agent.py")]
    if args.server:
        cmd += ["--server", args.server]
    if args.token:
        cmd += ["--token", args.token]
    if args.name:
        cmd += ["--name", args.name]
    print(f"  agent: {' '.join(Path(c).name if os.sep in c else c for c in cmd)}")
    return subprocess.Popen(cmd, cwd=str(HERE))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fps", type=int, default=QUIET_FPS,
                    help=f"frame cap while idling (default {QUIET_FPS})")
    ap.add_argument("--profile", type=int, default=1,
                    help="VRChat profile number; keeps this separate from "
                         "your own login (default 1)")
    ap.add_argument("--priority", type=int, default=-2,
                    choices=[-2, -1, 0, 1, 2],
                    help="-2 idle … 2 high (default -2)")
    ap.add_argument("--affinity", default=QUIET_AFFINITY,
                    help="hex CPU mask, e.g. 3 for two threads, FF for eight; "
                         "empty to leave alone")
    ap.add_argument("--vrchat", default="",
                    help="path to VRChat's launch.exe (not VRChat.exe)")
    ap.add_argument("--no-launch", action="store_true",
                    help="don't start VRChat; just run the agent beside the "
                         "client you already have open")
    ap.add_argument("--restart", action="store_true",
                    help="relaunch VRChat if it exits")
    ap.add_argument("--from-source", action="store_true",
                    help="run agent.py even if the packaged .exe is present")
    ap.add_argument("--server", default="", help="passed to the agent")
    ap.add_argument("--token", default="", help="passed to the agent")
    ap.add_argument("--name", default="", help="passed to the agent")
    args = ap.parse_args()

    if os.name != "nt":
        print("VRChat only runs on Windows; this launcher is Windows-only.")
        return 1

    vrchat = None
    if not args.no_launch:
        vrchat = Path(args.vrchat) if args.vrchat else find_vrchat()
        if not vrchat or not vrchat.is_file():
            print("Couldn't find VRChat. Pass --vrchat "
                  r'"D:\SteamLibrary\steamapps\common\VRChat\launch.exe"')
            return 1

    print("Roster host — VRChat runs minimised and throttled, not headless.")
    print("VRChat has no headless mode, and the roster only exists because a")
    print("real client is in the instance. This is the quiet next best thing.")
    print()

    client = agent = None
    try:
        if vrchat:
            print(f"Starting {vrchat.name} (profile {args.profile})")
            client = launch_vrchat(vrchat, args)
            if not wait_for_client(time.time() + 180):
                print("  VRChat never wrote a log. Is it stuck on the login "
                      "screen? Log in once on this profile, then rerun.")
        else:
            print("Using the VRChat client already running.")

        print()
        print("Starting the roster agent")
        agent = start_agent(args)

        print()
        print("Both running. Ctrl+C to stop. Join the instance you moderate;")
        print("the agent reports whoever is in it.")
        while True:
            time.sleep(3)
            if agent.poll() is not None:
                print(f"\nThe agent exited ({agent.returncode}). Restarting it.")
                time.sleep(5)
                agent = start_agent(args)
            if client and not game_running():
                if args.restart:
                    print("\nVRChat exited. Relaunching.")
                    client = launch_vrchat(vrchat, args)
                    wait_for_client(time.time() + 180)
                    continue
                # Without a client there is no log, so the agent would sit
                # reporting a room that nobody is in.
                print("\nVRChat exited. Stopping the agent too.")
                break
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        if agent and agent.poll() is None:
            print("  closing agent")
            try:
                agent.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                pass
            try:
                agent.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent.kill()
        if client and game_running():
            # The launcher process itself exited long ago, so this has to ask
            # Windows to close the game by name.
            print("  closing VRChat")
            try:
                subprocess.run(["taskkill", "/im", "VRChat.exe"],
                               capture_output=True, timeout=20)
            except (OSError, subprocess.SubprocessError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
