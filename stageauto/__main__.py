"""python -m stageauto [--run] [--no-open]
Lance le tableau de bord (http://127.0.0.1:8765). --run : démarre un passage (ou le demande à l'instance déjà ouverte)."""
import sys

from .config import DATA, IS_WIN, PORT


def _without_console():
    """pythonw.exe (StageAuto.bat) n'a pas de console : sys.stdout et sys.stderr valent None, ce qui fait planter le
    serveur web au démarrage. On les redirige vers un fichier pour que tout s'affiche dans console.log."""
    if sys.stdout is None or sys.stderr is None:
        f = open(DATA / "console.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


_without_console()

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import urllib.request  # noqa: E402
import webbrowser  # noqa: E402


def alert(msg: str):
    """Message visible même sans console (boîte de dialogue Windows)."""
    print(msg, file=sys.stderr)
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "StageAuto", 0x10)
        except Exception:
            pass


def already_running() -> bool:
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=2)
        return b'"state"' in r.read(400)
    except Exception:
        return False


def port_free() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def post(path):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=b"{}", headers={"content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def open_ui():
    url = f"http://127.0.0.1:{PORT}/"
    if IS_WIN:
        for exe in (os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
            if os.path.exists(exe):
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
        return 0
    if not port_free():
        alert(f"Le port {PORT} est déjà utilisé par un autre programme : StageAuto ne peut pas démarrer.\n"
              "Redémarre l'ordinateur, ou ferme le programme qui utilise ce port.")
        return 1
    try:
        import uvicorn
        from . import server
    except Exception:
        traceback.print_exc()
        alert("StageAuto n'a pas pu démarrer (module manquant ?). Relance install.bat.\n\n"
              f"Détails : {DATA / 'console.log'}\n\n{traceback.format_exc(limit=2)[-600:]}")
        return 1

    def later():
        for _ in range(180):
            if already_running():
                break
            time.sleep(0.5)
        else:
            alert(f"Le serveur de StageAuto ne répond pas. Détails : {DATA / 'console.log'}")
            return
        if not a.no_open:
            open_ui()
        if a.run:
            time.sleep(8)  # laisse l'extension se reconnecter
            try:
                post("/api/run")
            except Exception:
                traceback.print_exc()
    threading.Thread(target=later, daemon=True).start()
    try:
        # log_config=None : pas de configuration de journalisation d'uvicorn (elle exige une console) ;
        # ses messages vont dans stageauto.log comme le reste.
        uvicorn.run(server.app, host="127.0.0.1", port=PORT, log_level="warning", ws="auto", log_config=None)
    except Exception:
        traceback.print_exc()
        alert(f"StageAuto s'est arrêté sur une erreur. Détails : {DATA / 'console.log'}\n\n{traceback.format_exc(limit=2)[-600:]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
