"""Screenshot of the VRChat window at the moment a trigger fires.

With nameplates enabled this documents who was actually in view — the roster
from the log tells you who was in the instance, the screenshot narrows it to
who was in front of you.

Uses PrintWindow (PW_RENDERFULLCONTENT) so the capture shows VRChat's own
content even when other windows cover it — while in VR the desktop mirror
window is usually buried. Deliberately NO full-screen fallback: a grab of the
desktop could contain private windows, which must never end up in an
incident file. If VRChat can't be captured, the incident just has no
screenshot.
"""

import ctypes
from ctypes import wintypes

from autoclip import HERE

SHOTS_DIR = HERE / "incident_shots"

PW_RENDERFULLCONTENT = 2
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def grab_vrchat_window(inc_id: str) -> str | None:
    """Save a PNG of the VRChat window's content. Returns the path or None."""
    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    hwnd = user32.FindWindowW(None, "VRChat")
    if not hwnd:
        return None
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w < 100 or h < 100 or r.left < -20000:   # -32000 => minimized
        return None

    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbm = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    img = None
    try:
        gdi32.SelectObject(hdc_mem, hbm)
        if user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT):
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h          # negative => top-down rows
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = BI_RGB
            buf = ctypes.create_string_buffer(w * h * 4)
            if gdi32.GetDIBits(hdc_mem, hbm, 0, h, buf,
                               ctypes.byref(bmi), 0):
                img = Image.frombuffer("RGB", (w, h), buf.raw, "raw",
                                       "BGRX", 0, 1)
    finally:
        gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)

    if img is None or img.getbbox() is None:    # None bbox => all black
        return None
    SHOTS_DIR.mkdir(exist_ok=True)
    path = SHOTS_DIR / f"{inc_id}.png"
    img.save(path)
    return str(path)
