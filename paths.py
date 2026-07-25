"""Where the suite keeps its files.

Split out of autoclip.py so the web server can reach the database without
importing the capture stack (numpy/scipy/vosk/pyaudiowpatch are Windows-only
and irrelevant to a server that just serves records).
"""

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Overridable so the server can run against a copy of the store (or a volume
#: mount) without moving the rest of the tree.
DB_PATH = Path(os.environ.get("MODTOOL_DB") or (HERE / "modtool.db"))

#: Screenshots taken at trigger time; the web app serves them from here.
SHOTS_DIR = Path(os.environ.get("MODTOOL_SHOTS") or (HERE / "incident_shots"))
