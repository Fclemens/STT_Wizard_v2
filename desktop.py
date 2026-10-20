"""Desktop entry point: the app in its own window (Edge WebView2).

Runs the FastAPI server on a free localhost port in a background thread and
shows it with pywebview. This is what ``STT Wizard.exe`` starts; for
development, ``python desktop.py`` works too.
"""

from __future__ import annotations

import ctypes
import faulthandler
import hashlib
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request

from paths import APP_DIR

APP_NAME = "STT Wizard"
LOG_MAX_BYTES = 5 * 1024 * 1024

LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{height:100%;margin:0;background:#f5f0e8;color:#1d1a16;font:15px 'Segoe UI',sans-serif}
body{display:grid;place-items:center} .box{text-align:center}
.bar{width:220px;height:6px;margin:18px auto 0;border-radius:6px;background:rgba(50,38,20,.1);overflow:hidden}
.bar::after{content:"";display:block;width:35%;height:100%;background:#0e7c66;border-radius:6px;animation:s 1.2s ease-in-out infinite}
@keyframes s{0%{transform:translateX(-100%)}100%{transform:translateX(300%)}}
</style></head><body><div class="box"><b>STT Wizard</b><div class="bar"></div></div></body></html>"""


def setup_logging() -> None:
    """A windowed exe has no console: send stdout/stderr to logs/app.log."""
    logs = APP_DIR / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / "app.log"
    if log_path.exists() and log_path.stat().st_size > LOG_MAX_BYTES:
        log_path.replace(logs / "app.previous.log")
    stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives for the process
    sys.stdout = sys.stderr = stream
    faulthandler.enable(stream)
    print(f"\n=== {APP_NAME} started {time.strftime('%Y-%m-%d %H:%M:%S')} (folder: {APP_DIR}) ===")


def message_box(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)  # MB_ICONINFORMATION


def already_running() -> bool:
    """One instance per app folder (same data, same audio devices)."""
    folder_id = hashlib.sha1(str(APP_DIR).lower().encode()).hexdigest()[:16]  # str hash() is salted per process
    name = f"Local\\STTWizard-{folder_id}"
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    already = ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    globals()["_mutex"] = handle  # keep the handle alive for the process lifetime
    return already


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server(threading.Thread):
    def __init__(self, port: int) -> None:
        super().__init__(name="uvicorn", daemon=True)
        import uvicorn

        from app import app

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
        self.url = f"http://127.0.0.1:{port}/"

    def run(self) -> None:
        self.server.run()

    def wait_ready(self, timeout: float = 60) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline and self.is_alive():
            try:
                urllib.request.urlopen(self.url + "api/settings", timeout=1)
                return True
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.2)
        return False


def confirm_close(window) -> bool:
    """Called when the window is closing; False keeps it open."""
    import recording
    import session_store

    try:
        active = recording.active_recording()
        if active:
            if not window.create_confirmation_dialog(APP_NAME, "A recording is in progress.\n\nStop and save it, then quit?"):
                return False
            recording.stop(active, transcribe=False)
            for _ in range(600):  # mixing the tracks can take a moment on long recordings
                session = session_store.get_session(active)
                if not session or session.get("status") != "recording":
                    break
                time.sleep(0.1)
            return True
        busy = [s for s in session_store.list_sessions() if s["status"] in ("transcribing", "reviewing", "analyzing")]
        if busy:
            return bool(window.create_confirmation_dialog(
                APP_NAME, "Transcription or review is still running and will be interrupted.\n\nQuit anyway?"))
    except Exception:  # noqa: BLE001 - never block quitting because of a bug here
        traceback.print_exc()
    return True


def main() -> None:
    setup_logging()
    if already_running():
        message_box("STT Wizard is already running from this folder.")
        return

    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True
    window = webview.create_window(APP_NAME, html=LOADING_HTML, width=1360, height=900, min_size=(900, 600))
    window.events.closing += lambda: confirm_close(window)

    def boot() -> None:
        try:
            server = Server(free_port())
            server.start()
            if not server.wait_ready():
                raise RuntimeError("the local server did not start")
            print(f"Serving on {server.url}")
            window.load_url(server.url)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            window.load_html(
                f"<body style='font:15px Segoe UI;padding:40px'><h2>STT Wizard could not start</h2>"
                f"<p>{exc}</p><p>Details are in <code>{APP_DIR / 'logs' / 'app.log'}</code>.</p></body>"
            )

    webview.start(boot, gui="edgechromium", private_mode=False, storage_path=str(APP_DIR / "webview-cache"))
    print("Window closed; exiting.")
    sys.stdout.flush()
    os._exit(0)  # don't wait for worker threads or the CUDA runtime to tear down


if __name__ == "__main__":
    main()
