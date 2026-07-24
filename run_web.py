"""Start the web server.

    python run_web.py                 # host/port from web_config.json
    python run_web.py --port 9000     # override
    python run_web.py --init          # write a starter web_config.json

Serves the same modtool.db the desktop app uses. See README-web.md.
"""

import argparse
import secrets
import sys

import uvicorn

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
    ap.add_argument("--reload", action="store_true",
                    help="auto-reload on code changes (development)")
    args = ap.parse_args()

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

    print(f"Mod Suite web on http://{host}:{port}")
    if args.reload:
        uvicorn.run("run_web:app_factory", host=host, port=port, reload=True,
                    factory=True)
    else:
        uvicorn.run(create_app(cfg), host=host, port=port)


def app_factory():
    """Entry point for `uvicorn run_web:app_factory --factory` and --reload."""
    return create_app()


if __name__ == "__main__":
    main()
