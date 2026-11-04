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
    if not IS_WIN:  # comme le serveur web : les connexions qui se terminent (TIME_WAIT) ne bloquent pas le port
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def running_code():
    try:
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=3).read()).get("code")
    except Exception:
        return None


def wait_port_free(seconds) -> bool:
    for _ in range(int(seconds * 4)):
        if port_free():
            return True
        time.sleep(0.25)
    return port_free()


def kill_port_owner():
    """Ancienne instance sans commande d'arrêt : on arrête le processus Python qui écoute sur le port."""
    if not IS_WIN:
        return
    flags = 0x08000000
    pids = set()
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen).OwningProcess"],
                             capture_output=True, text=True, timeout=20, creationflags=flags).stdout
        pids |= {x.strip() for x in out.split() if x.strip().isdigit()}
    except Exception:
        pass
    if not pids:
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=20, creationflags=flags).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{PORT}") and parts[2] in ("0.0.0.0:0", "[::]:0"):
                    pids.add(parts[-1])
        except Exception:
            pass
    for pid in pids:
        try:
            info = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                  timeout=20, creationflags=flags).stdout.lower()
            if "python" in info:
                subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=20, creationflags=flags)
        except Exception:
            pass


def stop_old_instance() -> bool:
    try:
        post("/api/quit")
    except Exception:
        pass
    if wait_port_free(12):
        return True
    kill_port_owner()
    return wait_port_free(10)


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
                    # onglet dans ton Chrome déjà ouvert (pas de fenêtre en plus) ; réglage dashboard_window pour l'ancienne
                    # fenêtre séparée
                    from .config import load_settings
                    if load_settings().get("dashboard_window", False):
                        subprocess.Popen([exe, f"--app={url}", "--window-size=1280,900"])
                    else:
                        subprocess.Popen([exe, url])
                    return
                except OSError:
                    continue
    webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser(prog="stageauto")
    ap.add_argument("--run", action="store_true", help="démarrer un passage maintenant")
    ap.add_argument("--no-open", action="store_true", help="ne pas ouvrir le tableau de bord")
    ap.add_argument("--restart", action="store_true", help="(interne) attendre l'arrêt de l'instance précédente")
    a = ap.parse_args()
    from . import CODE
    if a.restart:
        wait_port_free(30)
    elif already_running() and running_code() != CODE:
        # nouvelle version décompressée par-dessus : l'ancienne instance est arrêtée, la nouvelle démarre
        print("Nouvelle version de StageAuto : redémarrage", file=sys.stderr)
        if not stop_old_instance():
            alert("Une ancienne version de StageAuto tourne encore et n'a pas pu être arrêtée.\n"
                  "Ouvre le Gestionnaire des tâches, termine « pythonw.exe », puis relance StageAuto.bat.")
            return 1
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
