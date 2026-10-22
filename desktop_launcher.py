from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
import webbrowser

# A PyInstaller --windowed build on Windows has no console at all, so
# sys.stdout/sys.stderr are actually None (not just redirected) - uvicorn's
# logging setup calls .isatty() on them to detect color support, which
# crashes with "'NoneType' object has no attribute 'isatty'" otherwise.
# macOS's windowed builds still get valid (if inert) stream objects, so this
# never engages there.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# Must happen before `app` (and transitively live.auth/overlay.protocol/
# overlay.render/monitor/clips/overlay.compose) is imported, so every
# sync_playwright() call picks up the bundled Chromium, and every
# imageio_ffmpeg.get_ffmpeg_exe() call picks up the bundled ffmpeg, instead
# of trying to find/download their own copies. Both libraries already
# support exactly this override via an env var, which is far more robust
# inside a frozen bundle than relying on their own internal
# package-relative/system-PATH auto-detection.
if getattr(sys, "frozen", False):
    _ffmpeg_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(sys._MEIPASS, "ms-playwright"))
    os.environ.setdefault("IMAGEIO_FFMPEG_EXE", os.path.join(sys._MEIPASS, _ffmpeg_name))

import uvicorn

from app import app

HOST = "127.0.0.1"
PORT = 8765


def _already_running() -> bool:
    try:
        urllib.request.urlopen(f"http://{HOST}:{PORT}/", timeout=1)
        return True
    except Exception:
        return False


def _open_browser() -> None:
    time.sleep(1.2)
    webbrowser.open(f"http://{HOST}:{PORT}")


def _run_tray_icon() -> None:
    # macOS: a windowless GUI app still shows in the Dock, so Cmd+Q / right-
    # click-Dock-icon-Quit already works with no extra code. Windows has no
    # equivalent - a --windowed app with no window is invisible everywhere
    # except Task Manager - so give it a system tray icon instead, purely so
    # there's a way to quit.
    import pystray
    from PIL import Image

    image = Image.open(os.path.join(sys._MEIPASS, "trayicon.png"))
    menu = pystray.Menu(
        pystray.MenuItem("Open eBay Live Tools", lambda icon, item: webbrowser.open(f"http://{HOST}:{PORT}")),
        pystray.MenuItem("Quit", lambda icon, item: (icon.stop(), os._exit(0))),
    )
    pystray.Icon("eBay Live Tools", image, "eBay Live Tools", menu).run()


def main() -> None:
    if _already_running():
        # Double-launching the .app (e.g. double-clicked twice, or opened
        # again while already running) - just re-open the existing instance
        # instead of crashing trying to bind an already-taken port.
        webbrowser.open(f"http://{HOST}:{PORT}")
        return
    threading.Thread(target=_open_browser, daemon=True).start()
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        threading.Thread(target=_run_tray_icon, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
