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
    # dashboard.sqlite3 / network.json must live next to the real exe,
    # not inside that temporary extraction folder, so they persist
    # across runs instead of being wiped out every time the app closes.
    PERSIST_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    PERSIST_DIR = BASE

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


def run_server():
    server.init()
    print(f"Game For Sale listening on http://{server.HOST}:{server.PORT}", flush=True)
    try:
        print("Panel address (QR):", server.panel_url(), flush=True)
    except Exception:
        pass
    http.server.ThreadingHTTPServer((server.HOST, server.PORT), server.H).serve_forever()


def main():
    port = server.PORT
    threading.Thread(target=run_server, daemon=True).start()
    wait_until_ready(server.HOST, port)
    url = f"http://127.0.0.1:{port}"

    try:
        import webview  # pywebview: a real, chrome-less app window

        window = webview.create_window("Game For Sale", url, width=1360, height=860, min_size=(900, 600))
        webview.start()
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
