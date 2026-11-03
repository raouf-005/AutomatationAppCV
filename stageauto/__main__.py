"""python -m stageauto [--run] [--no-open]
Lance le tableau de bord (http://127.0.0.1:8765). --run : démarre un passage (ou le demande à l'instance déjà ouverte)."""
import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from .config import IS_WIN, PORT


def already_running() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=2)
        return True
    except Exception:
        return False


def post(path):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=b"{}", headers={"content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def open_ui():
    url = f"http://127.0.0.1:{PORT}/"
    if IS_WIN:
        for exe in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1280,900"])
                return
            except OSError:
                continue
    webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser(prog="stageauto")
    ap.add_argument("--run", action="store_true", help="démarrer un passage maintenant")
    ap.add_argument("--no-open", action="store_true", help="ne pas ouvrir le tableau de bord")
    a = ap.parse_args()
    if already_running():
        if a.run:
            print(post("/api/run"))
        if not a.no_open:
            open_ui()
        return
    import uvicorn
    from . import server

    def later():
        for _ in range(60):
            if already_running():
                break
            time.sleep(0.5)
        if not a.no_open:
            open_ui()
        if a.run:
            time.sleep(8)  # laisse l'extension se reconnecter
            post("/api/run")
    threading.Thread(target=later, daemon=True).start()
    uvicorn.run(server.app, host="127.0.0.1", port=PORT, log_level="warning", ws="auto")


if __name__ == "__main__":
    sys.exit(main())
