# Putting the Quest agent on your moderators' headsets

Invite-only, through the Meta Horizon Store: the app is in your Developer
Dashboard, builds go only to accounts you invite, and it is never publicly
listed. No review queue, no store art, no age rating, no privacy policy.

Written for [`quest-agent/`](../quest-agent/) against Meta's current
requirements (checked 2026-08-16). Everything that could be run here has been;
the steps needing a Meta account are marked **you**.

## What you actually have to do

Four things, three of them once ever.

1. **you, once** — make a Meta developer account and an organisation at
   <https://developers.meta.com>. This gates everything else and can take a
   day, so start it first.
2. **you, once** — create the app in the dashboard. Pick **Meta Quest**, and
   leave it unlisted; you never submit it for review.
3. **you, once** — from the app's **Development → API** page, copy the App ID
   and App Secret into `quest-agent/release.local.json` (there is a
   `release.example.json` to copy). Download
   [ovr-platform-util](https://developers.meta.com/horizon/downloads/package/platform-utils-cli-pc/)
   and put it on your PATH or next to the script.
4. **you, each release** — run this:

   ```bash
   python quest-agent/release.py
   ```

   That is the whole release. It makes the signing key the first time, bumps
   the version, builds a signed APK, checks the signature, and uploads it to
   the ALPHA channel — the invite-only one.

Then, once, in the dashboard: **Distribution → Release Channels → ALPHA →
Email Invite Users**, or copy the invite URL and post it wherever your staff
already talk. Anyone on that list installs the app from their library like
anything else; anyone not on it cannot see it exists.

Meta has no API for invites, so that page is the one bit of clicking that
cannot be scripted away.

## What the script refuses to do

`ALPHA` is the default and `store` is guarded: pushing to the public channel
needs `--channel store --yes-public`, spelled out, on purpose. Invite-only
should be what happens when nobody is paying attention.

It also refuses to upload an APK signed with the throwaway key I used to prove
the signing path, so a test key cannot become your app's identity by accident.

## The one thing you cannot undo

The first run makes `quest-agent/upload.jks` and `keystore.properties`, and
prints a warning about them. **Back both up somewhere that is not that PC.**
Meta ties the app to that key. Lose it and the app can never be updated again
under that package name — no reset, no recovery, no appeal.

The package name is the other permanent choice: `com.vrcmodsuite.rosteragent`,
changeable in `quest-agent/app/build.gradle.kts` only *before* the first
upload.

## Two gates, not one

Being invited to the channel gets somebody the app. It does not get them
anything else: the agent does nothing until it is paired, and a pairing key
only exists when a signed-in moderator opens the pairing link — which requires
being in your staff group. Admins can revoke any agent's key from the panel
afterwards.

So install and use are gated separately, and neither implies the other.

## After the first install, test it

Nothing here has run on a headset yet, and one thing genuinely might not work:

- Does the service keep reporting while VRChat is immersive? This is the open
  question the whole design rests on. If it stops the moment VRChat takes
  over, the fallback is a panel left open beside it.
- Does the folder picker reach Documents › Logs?
- With VRChat's logging set to **Full** (Quick Menu → Settings → Debug — Quest
  ships on Errors Only), does Screening fill up?

## Updating later

Same command. The version bump, the build, the signature check and the upload
all happen again, and the invite URL's 90-day clock resets every time you
upload.

```bash
python quest-agent/release.py --notes "what changed"
```

## If you ever do want a public listing

It is the same APK plus homework: store art, IARC age rating, a data use
checkup, a real privacy policy URL, and a permission review for
`POST_NOTIFICATIONS` (the honest answer: it is the ongoing notification for a
foreground service the user starts themselves, not messaging — and the app
deliberately does not request All-files access, taking a folder grant through
the system picker instead). Meta asks you to submit about two weeks ahead of
any target date.

For a tool that does nothing without a key from your own panel, that is a lot
of ceremony for no extra reach.

## Sources

- [Uploading your Meta Quest apps](https://developers.meta.com/horizon/resources/publish-upload-overview/) — APK only, 1 GB, must be signed
- [OVR Platform Utility](https://developers.meta.com/horizon/resources/publish-reference-platform-command-line-utility/) — the upload command and where the App ID/Secret live
- [Add users to a release channel](https://developers.meta.com/horizon/resources/publish-release-channels-add-users/) — email and URL invites, 200 users by default, 2,500 max, 90-day URLs
- [Submitting your app](https://developers.meta.com/horizon/resources/publish-submit/) — what public review involves
- [Review-requiring Android permissions](https://developers.meta.com/horizon/resources/permissions-review-required/) — POST_NOTIFICATIONS, All-files access
- [Application manifests for release builds](https://developers.meta.com/horizon/resources/publish-mobile-manifest/) — the manifest checklist the app already meets
