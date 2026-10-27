#!/usr/bin/env python3
"""Launcher for the packaged (PyInstaller) build.

Starts the existing server.py in-process and opens a standalone native
window (no browser chrome, no address bar, no tabs, and -- this build --
no console window either). The app only shows up as its own window plus
a system tray icon; everything else runs silently in the background and
is visible in Task Manager as "GameForSale.exe".

Falls back to the system's default browser only if the native-window
toolkit (pywebview / the Windows WebView2 runtime) isn't available.
"""
import os
import sys
import threading
import time
import socket
import webbrowser
import http.server

# In a PyInstaller --windowed build there is no console, so sys.stdout /
# sys.stderr are None. Any print()/traceback.print_exc() anywhere in the
# codebase (including inside server.py's own exception handlers) would
# then crash with "NoneType has no attribute 'write'" instead of just
# being silently skipped. Redirect both to os.devnull up front so the
# rest of the code can print() freely and nothing ever sees a crash
# because of it; our own log() function below writes to a real file.
if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w")
    sys.stdout = sys.stdout or _devnull
    sys.stderr = sys.stderr or _devnull

# When frozen by PyInstaller (--onefile), data files are unpacked into a
# temporary folder at sys._MEIPASS for the lifetime of the process.
if getattr(sys, "frozen", False):
    BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

# Writable state (dashboard.sqlite3, network.json, logs, WebView2's own
# profile) must NOT live next to the exe: when installed into Program
# Files (the default install location), Windows blocks a normal,
# non-admin process from creating new files there. Use the per-user
# AppData folder instead -- always writable without elevation, and still
# persists across runs on this same Windows account.
if os.name == "nt":
    appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
else:
    appdata = None

if appdata:
    PERSIST_DIR = os.path.join(appdata, "GameForSale")
elif getattr(sys, "frozen", False):
    PERSIST_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    PERSIST_DIR = BASE
os.makedirs(PERSIST_DIR, exist_ok=True)

sys.path.insert(0, BASE)
os.chdir(PERSIST_DIR)

os.environ.setdefault("DASHBOARD_PORT", "8080")

LOG_PATH = os.path.join(PERSIST_DIR, "launcher.log")


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fatal_error(title, message):
    """No console window exists in this build, so on a fatal error: log
    it to disk and show a real Windows message box instead of failing
    silently (which is what would otherwise happen -- the process would
    just vanish with nothing visible anywhere)."""
    log(f"FATAL: {title}: {message}")
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                f"{message}\n\nDetails were saved to:\n{LOG_PATH}",
                f"Game For Sale - {title}",
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass
    sys.exit(1)


try:
    import server  # noqa: E402  (must come after sys.path/env setup above)
except Exception as exc:
    import traceback

    fatal_error("Failed to start", f"Could not load the app: {exc!r}\n\n{traceback.format_exc()}")
    raise

# server.py resolves BASE/DB/NET_FILE from its own module-level __file__
# at import time, which is not reliable once frozen. Point every one of
# them explicitly instead of trusting that resolution:
#   - BASE (read-only assets: static/, vendor/, catalog.json) -> the
#     PyInstaller extraction folder, where --add-data placed them.
#   - DB / NET_FILE (writable state) -> AppData, so they persist across
#     runs AND are always writable regardless of install location.
server.BASE = BASE
server.DB = os.path.join(PERSIST_DIR, "dashboard.sqlite3")
server.NET_FILE = os.path.join(PERSIST_DIR, "network.json")

log(f"assets folder: {server.BASE}")
log(f"writable data folder: {PERSIST_DIR}")

if not os.path.isfile(os.path.join(server.BASE, "static", "index.html")):
    fatal_error(
        "Packaging problem",
        f"static/index.html was not found under:\n{server.BASE}\n\nThis build is broken -- please report this.",
    )


def wait_until_ready(host, port, timeout=30):
    target_host = "127.0.0.1" if host in ("0.0.0.0", "", None) else host
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((target_host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


server_error = []


def run_server():
    # A crash in this background thread does NOT propagate to the main
    # thread -- Python just prints it (nowhere, since there's no console
    # in this build) and the thread quietly dies, leaving
    # wait_until_ready() to time out with no clue why. Catch it
    # explicitly and hand the error back to main().
    try:
        server.init()
        log(f"Game For Sale listening on http://{server.HOST}:{server.PORT}")
        try:
            log(f"Panel address (QR): {server.panel_url()}")
        except Exception:
            pass
        http.server.ThreadingHTTPServer((server.HOST, server.PORT), server.H).serve_forever()
    except Exception as exc:
        import traceback

        log("Server thread crashed:\n" + traceback.format_exc())
        server_error.append(exc)


def load_tray_icon_image():
    from PIL import Image

    ico_path = os.path.join(server.BASE, "app_icon.ico")
    return Image.open(ico_path)


def run_tray(window, stop_event):
    """System tray icon (the "show hidden icons" area next to the
    clock): right-click to reopen the window or quit the app entirely.
    Runs in its own background thread for the whole life of the app."""
    try:
        import pystray
    except Exception as exc:
        log(f"Tray icon unavailable ({exc!r}); app still runs, just without a tray icon.")
        return

    def on_open(icon, item):
        try:
            window.show()
        except Exception:
            pass

    def on_quit(icon, item):
        icon.stop()
        stop_event.set()
        try:
            window.destroy()
        except Exception:
            pass
        os._exit(0)

    try:
        image = load_tray_icon_image()
    except Exception as exc:
        log(f"Could not load tray icon image ({exc!r}); using a blank icon.")
        from PIL import Image

        image = Image.new("RGB", (16, 16), "blue")

    menu = pystray.Menu(
        pystray.MenuItem("Open Game For Sale", on_open, default=True),
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon("GameForSale", image, "Game For Sale", menu)
    icon.run()


def main():
    port = server.PORT
    threading.Thread(target=run_server, daemon=True).start()
    ready = wait_until_ready(server.HOST, port)
    if not ready:
        if server_error:
            fatal_error("Server failed to start", f"{server_error[0]!r}\n\nSee {LOG_PATH} for details.")
        else:
            fatal_error("Server failed to start", f"Timed out waiting for it to come up.\n\nSee {LOG_PATH} for details.")
        return
    url = f"http://127.0.0.1:{port}"

    try:
        import webview  # pywebview: a real, chrome-less app window
    except Exception as exc:
        # No native-window toolkit available (e.g. WebView2 runtime
        # missing) -- fall back to opening the page in the default
        # browser rather than leaving the user with nothing. The server
        # keeps running in the background either way.
        log(f"Native window unavailable ({exc!r}); opening in the default browser instead.")
        webbrowser.open(url)
        while True:
            time.sleep(3600)

    # Keep WebView2's own profile/cache in our guaranteed-writable
    # AppData folder too (not wherever pywebview defaults to, which can
    # land under Program Files when installed there and either fail to
    # write or silently keep serving a cached broken page across runs).
    webview_data_dir = os.path.join(PERSIST_DIR, "webview_data")
    os.makedirs(webview_data_dir, exist_ok=True)

    window = webview.create_window("Game For Sale", url, width=1360, height=860, min_size=(900, 600))

    stop_event = threading.Event()

    def on_closing():
        # Clicking the window's [X] hides it to the tray instead of
        # quitting outright, so the local server (and anyone else on the
        # shop's network using it) keeps running. Use the tray icon's
        # "Quit" to actually exit.
        window.hide()
        return False  # cancel the real close

    window.events.closing += on_closing

    threading.Thread(target=run_tray, args=(window, stop_event), daemon=True).start()

    webview.start(storage_path=webview_data_dir, private_mode=False)
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        import traceback

        fatal_error("Crashed on startup", f"{exc!r}\n\n{traceback.format_exc()}")
