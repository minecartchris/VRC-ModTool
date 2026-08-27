"""Build the Quest agent and push it to your private release channel.

One command, so a release is not a checklist:

    python release.py

It makes the signing key the first time, bumps the version, builds a signed
APK, checks the signature, and uploads it to the invite-only channel. Nothing
here reaches the public store: that needs --channel store and a deliberate
--yes-public, because "invite only" should be what happens when you are not
paying attention. (This text is printed by --help, so it stays ASCII - a
Windows console turns anything else into boxes.)

What it cannot do for you: create the app in Meta's dashboard, and invite
people to the channel. Meta has no API for either. Both are one-time.
"""
import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRADLE_FILE = HERE / "app" / "build.gradle.kts"
KEYSTORE_PROPS = HERE / "keystore.properties"
CONFIG = HERE / "release.local.json"
APK = HERE / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"

#: Meta's own name for its first pre-release channel. Builds here go only to
#: accounts invited in the dashboard, and the app is not publicly listed.
DEFAULT_CHANNEL = "ALPHA"
PUBLIC_CHANNELS = {"store", "production", "live"}

DOWNLOAD = "https://developers.meta.com/horizon/downloads/package/platform-utils-cli-pc/"


def die(message: str) -> None:
    print(f"\n  {message}\n")
    raise SystemExit(1)


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=HERE, check=False, **kw)


def java_home() -> str:
    """A JDK to run keytool with. Gradle finds its own; this is for keytool."""
    for candidate in filter(None, [os.environ.get("JAVA_HOME"),
                                   r"C:\Program Files\Android\openjdk\jdk-21.0.8"]):
        if (Path(candidate) / "bin" / "keytool.exe").exists() or \
                (Path(candidate) / "bin" / "keytool").exists():
            return candidate
    return ""


def ensure_keystore() -> None:
    """Make the upload key once. Losing it ends the app under this name."""
    if KEYSTORE_PROPS.exists():
        return
    home = java_home()
    if not home:
        die("No JDK found for keytool. Set JAVA_HOME and run this again.")
    store = HERE / "upload.jks"
    password = secrets.token_urlsafe(24)
    keytool = str(Path(home) / "bin" / "keytool")
    print("  No signing key yet - making one.")
    result = run([keytool, "-genkeypair", "-v", "-keystore", str(store),
                  "-storepass", password, "-keypass", password,
                  "-alias", "upload", "-keyalg", "RSA", "-keysize", "2048",
                  "-validity", "10000",
                  "-dname", "CN=VRChat Roster Agent, O=Mod Suite, C=US"],
                 capture_output=True, text=True)
    if result.returncode != 0:
        die(f"keytool failed:\n{result.stderr.strip()[:400]}")
    KEYSTORE_PROPS.write_text(
        f"storeFile={store.as_posix()}\nstorePassword={password}\n"
        f"keyAlias=upload\nkeyPassword={password}\n", encoding="utf-8")
    print(f"""
  ------------------------------------------------------------------
  BACK THESE UP, somewhere that is not this PC:

      {store}
      {KEYSTORE_PROPS}

  They are the only way to ever update this app again. Meta ties the
  app to this key; there is no reset, no recovery and no appeal.
  ------------------------------------------------------------------
""")


def version_code() -> int:
    text = GRADLE_FILE.read_text(encoding="utf-8")
    match = re.search(r"versionCode = (\d+)", text)
    if not match:
        die("Couldn't find versionCode in app/build.gradle.kts")
    return int(match.group(1))


def bump_version() -> int:
    """Every upload needs a higher number than the last. One less thing."""
    text = GRADLE_FILE.read_text(encoding="utf-8")
    nxt = version_code() + 1
    text = re.sub(r"versionCode = \d+", f"versionCode = {nxt}", text, count=1)
    GRADLE_FILE.write_text(text, encoding="utf-8")
    return nxt


def gradlew() -> list:
    return [str(HERE / ("gradlew.bat" if os.name == "nt" else "gradlew"))]


def build() -> None:
    print("  Building a signed release APK...")
    if run(gradlew() + ["clean", "assembleRelease", "--quiet"]).returncode != 0:
        die("The build failed. Nothing was uploaded.")
    if not APK.exists():
        die(f"The build reported success but {APK.name} isn't there.")


def apksigner() -> str:
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") \
        or str(Path.home() / "AppData" / "Local" / "Android" / "Sdk")
    tools = sorted((Path(sdk) / "build-tools").glob("*"), reverse=True)
    for tool in tools:
        for name in ("apksigner.bat", "apksigner"):
            if (tool / name).exists():
                return str(tool / name)
    return ""


def verify() -> None:
    signer = apksigner()
    if not signer:
        print("  (no apksigner found - skipping the signature check)")
        return
    result = run([signer, "verify", "--print-certs", str(APK)],
                 capture_output=True, text=True)
    if result.returncode != 0:
        die(f"The APK isn't properly signed:\n{result.stdout.strip()[:400]}")
    who = next((line for line in result.stdout.splitlines()
                if "certificate DN" in line), "")
    if "Throwaway" in who:
        die("This is signed with the throwaway test key. Delete "
            "keystore.properties and run this again to make a real one.")
    print(f"  Signature OK - {who.strip() or 'signed'}")


def uploader() -> str:
    found = shutil.which("ovr-platform-util") or shutil.which("ovr-platform-util.exe")
    if found:
        return found
    local = HERE / "ovr-platform-util.exe"
    return str(local) if local.exists() else ""


def load_config() -> dict:
    if not CONFIG.exists():
        die(f"""Missing {CONFIG.name}. Make it once, from the dashboard at
  Development -> API:

      {{
        "app_id": "1234567890",
        "app_secret": "...",
        "channel": "{DEFAULT_CHANNEL}"
      }}

  It is gitignored. The secret is a password - treat it like one.""")
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"{CONFIG.name} isn't valid JSON: {e}")
    return {}


def upload(cfg: dict, channel: str, notes: str) -> None:
    tool = uploader()
    if not tool:
        die(f"""ovr-platform-util isn't on this PC. Download it once:

      {DOWNLOAD}

  Put it on your PATH, or drop it next to this script as
  ovr-platform-util.exe, and run this again. The APK is already built:

      {APK}""")
    print(f"  Uploading to the {channel} channel...")
    result = run([tool, "upload-quest-build",
                  "--app-id", str(cfg["app_id"]),
                  "--app-secret", str(cfg["app_secret"]),
                  "--apk", str(APK),
                  "--channel", channel,
                  "--age-group", cfg.get("age_group", "TEENS_AND_ADULTS"),
                  "--notes", notes])
    if result.returncode != 0:
        die("The upload failed - the message above is Meta's. The APK is "
            f"still here if you want to upload it by hand:\n      {APK}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="",
                        help=f"release channel (default {DEFAULT_CHANNEL})")
    parser.add_argument("--notes", default="", help="release notes")
    parser.add_argument("--no-upload", action="store_true",
                        help="build and check it, but don't send it anywhere")
    parser.add_argument("--yes-public", action="store_true",
                        help="required to push to the public store channel")
    args = parser.parse_args()

    cfg = {} if args.no_upload else load_config()
    channel = args.channel or cfg.get("channel") or DEFAULT_CHANNEL
    if channel.lower() in PUBLIC_CHANNELS and not args.yes_public:
        die(f"'{channel}' is the public store. This app is invite-only by "
            "default; add --yes-public if you really mean it.")

    ensure_keystore()
    code = bump_version()
    print(f"  Version {code}.")
    build()
    verify()
    if args.no_upload:
        print(f"\n  Built and checked, not uploaded:\n      {APK}\n")
        return
    upload(cfg, channel, args.notes or f"build {code}")
    print(f"""
  Done. Build {code} is on the {channel} channel.

  To let a moderator install it: dashboard -> Distribution -> Release
  Channels -> {channel} -> Email Invite Users, or copy the invite URL and
  share it once. The URL keeps working for 90 days, and every upload
  from this script resets that clock.
""")


if __name__ == "__main__":
    main()
