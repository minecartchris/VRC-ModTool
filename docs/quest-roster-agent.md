# A roster agent that runs on the Quest

Whether the screening roster can come from a headset instead of a PC, and
what it would take. Researched 2026-08-14; nothing here has been run on a
headset yet, and the three things that need a headset to answer are marked
**verify on device**.

> **It has since been built** — see [`quest-agent/`](../quest-agent/) for the
> app and its README. The three questions below are still the three questions;
> they are what the first install on a real headset is for.

## The short answer

Yes, probably — and the app itself is small. The roster does not come from
VRChat's API, it comes from VRChat's own log file, and VRChat on Quest writes
one. Everything the current PC agent does is four regexes and an HTTP POST;
none of that gets harder on Android.

The risk is not the app. It is two platform questions: whether Horizon OS
lets a sideloaded service keep reading a file while VRChat is immersive, and
whether a sideloaded app can read `Documents/Logs` at all under Android 14
storage rules. Both look answerable, neither is answered here.

## Why it has to read the log

VRChat's API will tell you how many people are in an instance, never who. The
PC agent exists because the only list of names is the one VRChat writes into
its own log as people join and leave. That constraint is the same on Quest, so
a Quest agent is the same idea pointed at a different file.

## What is already true on Quest

**The log exists and is in shared storage.** VRChat's help pages put Quest
output logs in "the Documents/Logs folder located in the device root" — that
is `/sdcard/Documents/Logs`, not the sandboxed `Android/data/<package>` that
Android 11 walled off. That matters more than anything else here: reading
another app's `Android/data` from an on-device app is effectively blocked on
modern Android, and reading shared storage is not.

**Full logging is off by default.** On Quest, VRChat ships set to *Errors
Only*; the options are Full, Errors Only and Off, under Quick Menu →
Settings → Debug. Join and leave lines only appear on **Full**. Every
moderator using this would have to flip that switch once, and it is worth
assuming an update can reset it — the app should notice a log that has stopped
producing join lines and say so, rather than reporting an empty room.

**Horizon OS is Android 14.** So the app targets a modern SDK and lives under
scoped storage. Reading `Documents/` needs `MANAGE_EXTERNAL_STORAGE` ("All
files access"), which a sideloaded app can request and the user grants in a
2D settings panel. **Verify on device** that Horizon OS actually exposes that
toggle; Meta has trimmed settings panels before.

**2D apps are allowed to run in the background.** Meta lists "background
running" among the features available to 2D Android apps on Horizon OS, and
since v69 a 2D panel can stay live beside a fully immersive app. That is the
whole basis for this working — but it describes panels, not headless
services. **Verify on device**: a foreground service (`dataSync` type, with
its notification) tailing a file while VRChat is immersive *and the panel is
closed*. If it only survives with the panel open, that is still usable — a
moderator can park the panel to one side — but it changes what we promise.

## What the app has to do

Almost nothing, which is the point:

1. Pair once. `POST /api/agent/pair/start`, then poll `/api/agent/pair/poll`
   until the moderator opens the one-time link while signed in. Identical to
   the desktop agent, so the panel needs no changes — the pairing screen, the
   revoke button and the per-instance screening all work as they already do.
2. Find the newest `output_log_*.txt` in `/sdcard/Documents/Logs`, read it
   from the top, then follow it.
3. Match four lines — `Entering Room`, `Joining wrld_…:instance`,
   `OnPlayerJoined`, `OnPlayerLeft` — exactly as `vrc_log.py` does.
4. Every ten seconds, `POST /api/sync/roster` with the `X-Sync-Token` header
   and `{client_id, client_name, roster: {world_name, world_id, instance_id,
   players}}`.

That is the entire protocol. The server already merges two agents in one
instance, already ignores instances that are not the group's, and already
treats a quiet agent as stale — so a Quest reporter joins the existing
picture rather than needing a new one.

**Verify on device**: that Quest log lines carry the same `[Behaviour]`
prefixes as the PC log, and how quickly Unity flushes them. If the format has
drifted the regexes need adjusting, and if it flushes lazily the roster lags.
Both are answered by one real `output_log_*.txt` off a headset — that file is
the single most useful thing to get before any code is written.

## If background execution turns out not to work

- **Keep the panel open.** With v69 multitasking the agent panel can sit
  beside VRChat. Least engineering, some friction for the moderator.
- **Pull the log over Wi-Fi ADB from a PC.** The headset needs developer mode
  on and wireless debugging paired; a PC on the same network runs a variant of
  the existing Python agent that reads the log via `adb exec-out cat` instead
  of the local filesystem. This needs a PC — but not VRChat on it, which is
  most of what the Quest agent is for. It is also the only option here that
  can be built and tested without owning the headset.
- **Rely on a PC moderator in the same instance.** Already works today: the
  screening page unions every live agent reporting the same room, so one PC
  mod covers everyone standing with them.

## Cost and what is needed to start

The app is a few hundred lines of Kotlin — a foreground service, a file
tailer, four regexes, one POST, and a pairing screen. The work is in the
platform, not the code: sideloading, storage permission, background
lifetime, and getting it onto each moderator's headset (SideQuest or `adb
install`; a Meta store listing is not worth pursuing for an internal tool).

To move from research to build:

1. A real `output_log_*.txt` from a Quest running VRChat with Full logging on.
2. Which headsets the moderators actually have, and their Horizon OS version.
3. An Android SDK on this machine — there is a JDK but no SDK and no `adb`,
   so nothing can currently be compiled or installed. That is a deliberate
   ask rather than something to install unannounced.

## Sources

- VRChat, [Where do I find my Output Logs and Crash Dumps?](https://help.vrchat.com/hc/en-us/articles/9521522810899-Where-do-I-find-my-Output-Logs-and-Crash-Dumps)
- VRChat, [VRChat Mobile: How do I access my Output Logs?](https://help.vrchat.com/hc/en-us/articles/19651108531859-VRChat-Mobile-How-do-I-access-my-Output-Logs)
- Meta, [Getting started with Android apps on Meta Horizon OS](https://developers.meta.com/horizon/documentation/android-apps/horizon-os-apps/)
- UploadVR, [Seamless multitasking in Horizon OS](https://www.uploadvr.com/seamless-multitasking-experimental-quest/)
- Android, [Scoped storage](https://source.android.com/docs/core/storage/scoped)
