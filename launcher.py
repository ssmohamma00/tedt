#!/usr/bin/env python3
"""Launcher for the packaged (PyInstaller) build.

Starts the existing server.py in-process and opens a standalone native
window (no browser chrome, no address bar, no tabs) pointed at the local
dashboard — so double-clicking the installed app feels like opening a
normal desktop program, not "go open your browser".

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

# When frozen by PyInstaller (--onefile), data files are unpacked into a
# temporary folder at sys._MEIPASS for the lifetime of the process.
if getattr(sys, "frozen", False):
    BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

# Writable state (dashboard.sqlite3, network.json) must NOT live next to
# the exe: when installed into Program Files (the default install
# location), Windows blocks a normal, non-admin process from creating
# new files there, so sqlite3 fails with "unable to open database file"
# and the whole app never starts. Use the per-user AppData folder
# instead -- always writable without elevation, and still persists
# across runs on this same Windows account.
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

import server  # noqa: E402  (must come after sys.path/env setup above)

# server.py resolves BASE/DB/NET_FILE from its own module-level
# __file__ at import time, which is not reliable once frozen (and in
# practice resolved to the wrong folder in testing, serving a blank
# "not found" page). Point every one of them explicitly instead of
# trusting that resolution:
#   - BASE (read-only assets: static/, vendor/, catalog.json) -> the
#     PyInstaller extraction folder, where --add-data placed them.
#   - DB / NET_FILE (writable state) -> next to the real exe, so data
#     survives between runs.
server.BASE = BASE
server.DB = os.path.join(PERSIST_DIR, "dashboard.sqlite3")
server.NET_FILE = os.path.join(PERSIST_DIR, "network.json")

print(f"[debug] writable data folder (server.DB's dir): {PERSIST_DIR}", flush=True)
print(f"[debug] assets folder (server.BASE): {server.BASE}", flush=True)
print(f"[debug] static/index.html exists: {os.path.isfile(os.path.join(server.BASE, 'static', 'index.html'))}", flush=True)
try:
    print(f"[debug] contents of assets folder: {os.listdir(server.BASE)}", flush=True)
except Exception as _e:
    print(f"[debug] could not list assets folder: {_e!r}", flush=True)

assert os.path.isfile(os.path.join(server.BASE, "static", "index.html")), (
    f"static/index.html not found under {server.BASE} -- packaging problem"
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
    # thread or raise in main() -- Python just prints it and the thread
    # quietly dies, leaving wait_until_ready() to time out with no clue
    # why. Catch it explicitly and hand the error back to main().
    try:
        server.init()
        print(f"Game For Sale listening on http://{server.HOST}:{server.PORT}", flush=True)
        try:
            print("Panel address (QR):", server.panel_url(), flush=True)
        except Exception:
            pass
        http.server.ThreadingHTTPServer((server.HOST, server.PORT), server.H).serve_forever()
    except Exception as exc:
        import traceback

        traceback.print_exc()
        server_error.append(exc)


def main():
    port = server.PORT
    threading.Thread(target=run_server, daemon=True).start()
    ready = wait_until_ready(server.HOST, port)
    if not ready:
        if server_error:
            raise RuntimeError(f"The local server failed to start: {server_error[0]!r}") from server_error[0]
        raise RuntimeError("The local server did not start within 30 seconds.")
    url = f"http://127.0.0.1:{port}"

    try:
        import webview  # pywebview: a real, chrome-less app window

        # Keep WebView2's own profile/cache in our guaranteed-writable
        # AppData folder too (not wherever pywebview defaults to, which
        # can land under Program Files when installed there and either
        # fail to write or silently cache a broken page across runs).
        webview_data_dir = os.path.join(PERSIST_DIR, "webview_data")
        os.makedirs(webview_data_dir, exist_ok=True)

        window = webview.create_window("Game For Sale", url, width=1360, height=860, min_size=(900, 600))
        webview.start(storage_path=webview_data_dir, private_mode=False)
        # webview.start() blocks until the window is closed; once the
        # user closes it, exit the whole process (the server thread is
        # a daemon thread, so it won't keep the app alive on its own).
        os._exit(0)
    except Exception as exc:
        # No native-window toolkit available (e.g. WebView2 runtime
        # missing) -- fall back to opening the page in the default
        # browser rather than leaving the user with nothing.
        print(f"[warn] native window unavailable ({exc}); opening in your browser instead.", flush=True)
        webbrowser.open(url)
        print("Close this window to stop the server.", flush=True)
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Without this, a crash just flashes the console window shut
        # (it was opened by double-clicking the exe in Explorer) before
        # anyone can read why. Keep it open so the error can be reported.
        import traceback

        traceback.print_exc()
        print("\nGame For Sale crashed on startup (see error above).")
        input("Press Enter to close this window...")
        sys.exit(1)
