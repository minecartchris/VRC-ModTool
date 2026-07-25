"""ASGI entry point for the reloader (and any external server).

Deliberately not run_web.py. The reloader has to re-import whatever module
holds the app, and when that module is also the `__main__` script the import
re-enters the CLI's main() — which calls uvicorn.run() again, so the
replacement worker never starts and the app silently stops updating. Keeping
the entry point in a plain module makes the re-import inert.

    uvicorn webapp.asgi:create --factory
"""

from webapp.server import create_app


def create():
    return create_app()
