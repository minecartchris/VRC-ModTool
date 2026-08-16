# Getting the Quest agent onto the Meta Horizon Store

Written for [`quest-agent/`](../quest-agent/), against Meta's current
requirements (checked 2026-08-16). Every command here has been run except the
ones that need a Meta developer account — those are marked.

## First, the decision worth making before any of it

There are two ways to get this onto moderators' headsets, and only one of them
involves the store at all.

**Sideloading.** You hand out the APK, they `adb install` it. No account, no
review, no listing, no privacy policy, no art. Updates are manual and it never
appears in anyone's library.

**The store, as a private release channel.** The app exists in the Developer
Dashboard, and builds go only to Meta accounts you invite by email or link —
200 by default, raisable to 2,500. No public listing. Installs and updates come
through the headset normally.

**The store, publicly listed.** Everything below, including review, age
rating, store art and a privacy policy — for a tool that only your staff can
actually use, since it does nothing without a paired key from your panel.

For an internal moderation tool the private channel is almost certainly the
right one: real installs and updates, no public listing, no review queue. The
steps below take you there, and then say what the extra mile to a public
listing costs.

## 1. Build a signed release APK

**Meta takes APKs, not app bundles** — up to 1 GB, signed with an Android
certificate. (The README used to say `bundleRelease`; that was wrong, and the
AAB it produces is of no use here.)

Make an upload key. This is the one irreversible step in the whole process:
lose this file or its passwords and you can never update the app again, under
that package name, ever.

```bash
keytool -genkeypair -v -keystore upload.jks -keyalg RSA -keysize 2048 -validity 10000 -alias upload
```

Put it somewhere backed up and outside the repo, then write
`quest-agent/keystore.properties` (already gitignored):

```properties
storeFile=C:/keys/upload.jks
storePassword=…
keyAlias=upload
keyPassword=…
```

Build:

```bash
./gradlew clean assembleRelease
```

The APK lands at `quest-agent/app/build/outputs/apk/release/app-release.apk` —
about 6 MB. Check it before uploading anything:

```bash
apksigner verify --print-certs -v app/build/outputs/apk/release/app-release.apk
```

It should say `Verifies` and name your certificate — if it names
"Throwaway Test Key" you have picked up the test key I used to prove the
signing path works; delete `keystore.properties` and write your own.

```bash
aapt dump badging app/build/outputs/apk/release/app-release.apk | head -3
```

Confirm `package: name='com.vrcmodsuite.rosteragent'` and the `versionCode`.
**Every upload needs a higher `versionCode`** than the last — bump it in
`quest-agent/app/build.gradle.kts` for each build you send.

## 2. Create the app in the Developer Dashboard

*(Needs a Meta developer account and an organization. Set that up first; it
gates everything else and can take a day.)*

At <https://developers.meta.com/horizon/manage>, create an app for Meta Quest.
Two things to get right at creation, because neither can be changed later:

- **The package name.** It is `com.vrcmodsuite.rosteragent` unless you change
  it in `app/build.gradle.kts` **before** the first upload. After publishing it
  is permanent.
- **The org it belongs to**, if you have more than one.

## 3. Upload the build

Three ways in; all do the same thing.

**Dashboard.** Your app → Distribution → Builds (or open a release channel) →
Upload → pick the APK. Easiest for a first upload.

**Command line**, which is what you want once this is routine. Get the tool
from Meta's downloads page, and the credentials from your app under
Development → API — treat the app secret like a password:

```bash
ovr-platform-util upload-quest-build --app-id <ID> --app-secret <SECRET> --apk app/build/outputs/apk/release/app-release.apk --channel ALPHA --age-group TEENS_AND_ADULTS --notes "First build"
```

**Meta Quest Developer Hub**, if you prefer a GUI — it wraps the same tool.

Upload to **ALPHA**, not Production. Production means the public store.

## 4. Put it on your moderators' headsets

In the Dashboard: Distribution → Release Channels → your channel → **Email
Invite Users**, comma-separated Meta account emails, or generate an invite URL
(URLs expire after 90 days unless a new build is uploaded).

An invited account gets the app in their library and installs it like anything
else. This is the allowlist you were asking about — combined with pairing,
someone would need both an invite to the channel *and* an approved pairing from
the panel before the app does anything at all.

Then actually test it, because nothing here has run on a headset yet:

- Does the service keep reporting while VRChat is immersive? This is the open
  question the whole design rests on.
- Does the folder picker reach Documents › Logs?
- With VRChat's logging set to Full, does Screening fill up?

## 5. Only if you want a public listing

Everything above stays true; this is the extra mile.

- **Store listing**: name, descriptions, category, icon, cover art,
  screenshots and/or video. The Dashboard has a cropping tool and an asset
  library, so aspect ratios are not the obstacle they look like.
- **Age rating** via the IARC questionnaire.
- **Data use checkup** — declare what data the app handles and why. For this
  app: VRChat display names and user ids of people in the moderator's current
  instance, sent to a server your group runs.
- **A privacy policy URL.** Required, and it has to be real. Say plainly what
  is sent, to whom, and that the app reads nothing but VRChat's log folder.
- **Permission review.** The manifest declares `POST_NOTIFICATIONS`, which
  Meta reviews case by case. The honest answer: it is the ongoing notification
  for a foreground service the user starts themselves, not messaging. Worth
  adding that the app deliberately does *not* request All-files access — it
  takes a folder grant through the system picker instead, which is why there
  is no storage permission to justify.
- **Content**: be explicit that it is a moderation tool for one group's staff
  and useless without their server. A reviewer who thinks it is a general
  VRChat utility will test it, find it does nothing, and reject it.

Then: app details, pricing (free), every metadata section green, **Submit for
Review**. Meta asks you to plan submission about two weeks ahead of any target
date.

## 6. Updating later

1. Bump `versionCode` (and `versionName` if it means anything to you).
2. `./gradlew clean assembleRelease`
3. Upload to the channel — same command, new `--notes`.
4. Promote the build when you are happy with it.

Same key every time. If you ever cannot sign with it, the app is finished
under that package name.

## What the manifest already satisfies

So you are not hunting for these during review — `quest-agent` already sets
`installLocation` to auto, `excludeFromRecents` on the launch activity, a
supported-devices entry covering Quest 2 / Pro / 3 / 3S, `debuggable` off in
release, minSdk 29 (the oldest any current Quest runs) and targetSdk 34
(inside the 32–36 band Meta allows for 2D apps), and marks both touchscreen
and headtracking as not required so a 2D app is not filtered off a headset
that has neither.

## Sources

- [Uploading your Meta Quest apps](https://developers.meta.com/horizon/resources/publish-upload-overview/) — APK only, 1 GB, signing
- [OVR Platform Utility](https://developers.meta.com/horizon/resources/publish-reference-platform-command-line-utility/) — the upload command
- [Add users to a release channel](https://developers.meta.com/horizon/resources/publish-release-channels-add-users/) — email and URL invites, limits
- [Submitting your app](https://developers.meta.com/horizon/resources/publish-submit/) — the review flow
- [Review-requiring Android permissions](https://developers.meta.com/horizon/resources/permissions-review-required/) — POST_NOTIFICATIONS, All-files access
- [Application manifests for release builds](https://developers.meta.com/horizon/resources/publish-mobile-manifest/) — manifest checklist
