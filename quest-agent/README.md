# VRChat Roster Agent — Quest

The roster agent, as an Android app for the headset. It reads VRChat's own log
on the Quest and posts who is in your instance to the mod panel, so a
moderator on standalone can feed Screening without a PC running anything.

It talks to the same two endpoints the desktop agent does, so the server needs
no changes: pair once against `/api/agent/pair/start`, then post to
`/api/sync/roster` every ten seconds.

## Status

**Built and tested as far as it can be without a headset.** The debug APK and
a signed release APK both build — the signing path was proved end to end with
a throwaway key, and `apksigner verify` accepts the result. The log parsing has
13 unit tests against the lines VRChat writes, including a live check of the
same patterns against a real 1 MB PC log (77 joins, all carrying user ids).

Three things need a headset to confirm, and all three are the platform rather
than the code:

1. **Whether the service keeps running while VRChat is immersive.** It is a
   `dataSync` foreground service, which is the kind Android keeps alive off
   screen, and Meta lists background running among what 2D apps may do — but
   Horizon OS is stricter than stock Android about this and nobody has run it
   there yet.
2. **Whether the log folder can be handed over.** Quest logs live in
   `Documents/Logs`, which the system folder picker can reach; the app never
   asks for All-files access (see below).
3. **Whether Quest log lines carry the same `[Behaviour]` prefixes as the PC
   log.** Same Unity logger, so probably — one real `output_log_*.txt` off a
   headset settles it, and the tests make the fix obvious if it has drifted.

Nothing here has run on a Quest yet. Treat the first install as a test.

## Why it asks for a folder instead of storage permission

The obvious way to read `Documents/Logs` is `MANAGE_EXTERNAL_STORAGE` — All
files access. Meta lists that permission as review-requiring, and says
"permitted uses include file management features". A roster agent is not a
file manager, so that route risks the store listing over a convenience.

Instead the moderator picks the folder once through the system picker and the
app keeps a persisted grant to that one directory. No storage permission is
declared at all. The permissions it does declare are `INTERNET`,
`ACCESS_NETWORK_STATE`, `FOREGROUND_SERVICE`,
`FOREGROUND_SERVICE_DATA_SYNC`, and `POST_NOTIFICATIONS` — of which only the
last needs review, and the service runs without it if refused.

## Building

Needs a JDK 17+ and an Android SDK with platform 34. Point `local.properties`
at the SDK (copy `local.properties.example`), then:

```bash
./gradlew testDebugUnitTest
```

```bash
./gradlew assembleDebug
```

The APK lands in `app/build/outputs/apk/debug/`.

## Installing on a headset

Developer mode on, headset plugged in and trusted, then:

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

It appears under Apps → Unknown Sources. Then, in the app:

1. Type the panel's address.
2. **Choose VRChat's Logs folder** — Documents › Logs on the headset.
3. **Pair with the panel.** It shows a code and a link; open the link on any
   device where you are signed in to the panel. A phone is easier to sign in
   on than a floating keyboard, and the key never appears on screen either way.
4. **Start reporting**, then put the headset on.

In VRChat itself: Quick Menu → Settings → Debug → **Logging: Full**. Quest
ships on Errors Only, and join lines only appear on Full. The app says so on
its own if it sees a log growing without any.

## Getting it onto moderators' headsets

Invite-only, through the store: the app is in the Developer Dashboard, builds
go to accounts you invite, and it is never publicly listed. One command:

```bash
python release.py
```

It makes the signing key the first time, bumps the version, builds a signed
APK, checks the signature, and uploads it to the invite-only ALPHA channel.
The public store needs `--channel store --yes-public`, spelled out, because
invite-only should be the default when nobody is paying attention.

Set-up is three one-time things — a Meta developer account, the app created in
the dashboard, and its App ID and Secret copied into `release.local.json` (see
`release.example.json`). Inviting people is one dashboard page; Meta has no
API for it. Full walkthrough:
[docs/horizon-store-upload.md](../docs/horizon-store-upload.md).

Meta takes **APKs, not app bundles** — signed, up to 1 GB. `assembleRelease`
produces one; `bundleRelease` builds an AAB that is of no use here. The
manifest already carries Meta's release checklist: `installLocation` auto,
`excludeFromRecents` on the launch activity, a supported-devices entry, no
`debuggable`, and touchscreen and headtracking both marked not required so a
2D app is not filtered off a headset that has neither.

**Back up `upload.jks` and `keystore.properties`** the moment the script makes
them. Meta ties the app to that key: lose it and this app can never be updated
again under its package name.

## Layout

| File | What it does |
| --- | --- |
| `RoomState` in `LogTail.kt` | The parsing. No Android in it, so it is unit tested. |
| `LogTail.kt` | Which file, how far in, and reading the new bytes through the folder grant. |
| `Api.kt` | The three calls: pair start, pair poll, post roster. |
| `RosterService.kt` | The foreground service that polls and posts every ten seconds. |
| `MainActivity.kt` | Setup panel: address, folder, pairing, start/stop, status. |
| `release.py` | One command to build, sign, version-bump and upload to the invite-only channel. |
