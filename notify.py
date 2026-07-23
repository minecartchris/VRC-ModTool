"""In-VR notifications: XSOverlay popups and VRChat OSC chatbox messages.

Both are fire-and-forget UDP to localhost, so calls never block and silently
do nothing if the target app isn't running.

Note: the OSC chatbox is PUBLIC — everyone near you in the instance can read
it. For moderation alerts prefer XSOverlay (or OVR Toolkit, which listens on
the same XSOverlay-compatible port), which only you see.
"""

import json
import socket

XSOVERLAY_PORT = 42069
VRC_OSC_PORT = 9000

_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def xsoverlay_notify(title: str, content: str = "", timeout: float = 5.0,
                     host: str = "127.0.0.1", port: int = XSOVERLAY_PORT) -> None:
    """Show a private overlay popup in the headset via XSOverlay's API."""
    msg = {
        "messageType": 1,          # 1 = notification popup
        "index": 0,
        "timeout": timeout,
        "height": 110.0,
        "opacity": 1.0,
        "volume": 0.6,
        "audioPath": "default",
        "title": title,
        "content": content,
        "useBase64Icon": False,
        "icon": "default",
        "sourceApp": "MedalModSuite",
    }
    try:
        _sock.sendto(json.dumps(msg).encode("utf-8"), (host, port))
    except OSError:
        pass


def _osc_pad(b: bytes) -> bytes:
    # OSC strings are null-terminated and padded to a multiple of 4 bytes;
    # when already aligned, a full 4 nulls are appended as the terminator.
    return b + b"\x00" * (4 - len(b) % 4)


def chatbox_message(text: str, host: str = "127.0.0.1",
                    port: int = VRC_OSC_PORT) -> None:
    """Put text in your VRChat chatbox via OSC. Visible to nearby players."""
    payload = (_osc_pad(b"/chatbox/input")
               + _osc_pad(b",sTF")                       # string, send-now, no sfx
               + _osc_pad(text.encode("utf-8")[:140]))   # chatbox caps ~144 chars
    try:
        _sock.sendto(payload, (host, port))
    except OSError:
        pass
