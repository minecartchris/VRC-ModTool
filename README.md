# VRChat Mod Suite (Medal auto-clipper)

Local moderation toolkit for VRChat. Listens to your speaker output,
transcribes it offline with Vosk, and when a trigger word/phrase is heard
(see `triggers.txt`) it presses your Medal clip hotkey and files an
**incident**: timestamp, transcript context, the world/instance you were in,
everyone present, and the Medal clip itself.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Then:

1. Download a Vosk model from https://alphacephei.com/vosk/models and unzip it
   into this folder (e.g. `vosk-model-small-en-us-0.15` for fast, or
   `vosk-model-en-us-0.22` for accurate).
2. Copy `config.example.json` → `config.json` and `triggers.example.txt` →
   `triggers.txt`, then edit to taste. (Both real files are gitignored so your
   settings and trigger list stay local.)
3. If you use the VRChat API features, set your own contact string in
   `USER_AGENT` at the top of `vrc_api.py`.

Run `gui.bat` (or `pythonw gui.py`).

## Tabs

- **Listener** — start/stop the audio watcher; pick capture device, model,
  hotkey, cooldown. Live transcript log and level meter.
- **Instance** — live view of your current world and player roster (names +
  `usr_` IDs), read from VRChat's local output log. "Copy roster" puts the
  whole list on the clipboard.
- **Screening** — age-check workflow over everyone in the instance. Each new
  player's private note and group memberships are looked up once via the
  VRChat API and cached locally in `screening_db.json`; the list refreshes as
  people join/leave. Live verified/unverified counts, per-player **Over /
  Under** (logs an age incident) and **In Range** (tags the note), plus a
  configurable group filter whose members are auto-verified. The per-user
  cache lives in `modtool.db`.
- **Incidents** — one row per trigger. Select one to see a paste-ready
  report (world/instance IDs, transcript, roster, linked clip, and a
  screenshot of the VRChat window at the trigger moment — with nameplates
  on, that's who was in view). Buttons: Copy report, Save notes, Open clip,
  Open screenshot, Mark reported, Delete. Stored in `modtool.db`;
  screenshots in `incident_shots\`.
- **Settings** — in-VR notifications (XSOverlay/OVR Toolkit popup — private;
  VRChat chatbox — public, off by default), Medal clips folder, and an
  optional VRChat account login (used for user lookups; only the session
  cookie is stored, in `vrc_cookies.txt`).

## How pieces fit

| File | Role |
|---|---|
| `autoclip.py` | audio capture → Vosk transcription → hotkey engine (also a CLI) |
| `gui.py` | tabbed Tkinter app wiring everything together |
| `vrc_log.py` | tails `AppData\LocalLow\VRChat\VRChat\output_log_*.txt` for world/player state |
| `db.py` | one SQLite store (`modtool.db`) for incidents + the screening cache |
| `incidents.py` | incident records (SQLite via `db.py`) + Medal clip discovery |
| `report.py` | incident → paste-ready report text |
| `capture.py` | PrintWindow screenshot of VRChat at trigger time (works while occluded; never falls back to a desktop grab) |
| `notify.py` | XSOverlay popup + OSC chatbox (UDP, fire-and-forget) |
| `vrc_api.py` | minimal VRChat web API client (login, 2FA, user search) |

Settings persist in `config.json`; incidents and the screening cache persist in
`modtool.db` (a JSONL/JSON store from older versions is imported automatically
on first run). Everything runs locally; the only network calls are the optional
VRChat API login/lookups and the one-time Vosk model download.
