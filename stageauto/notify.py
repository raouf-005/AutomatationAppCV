"""Notifications : journal de l'interface, notification Windows et, si configuré, téléphone via ntfy.sh."""
import collections
import datetime as dt
import logging
import subprocess
import threading
import urllib.request

from .config import IS_WIN, LOG_PATH, load_settings

logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(message)s", encoding="utf-8")
LOG = collections.deque(maxlen=800)
ALERTS = collections.deque(maxlen=50)
_lock = threading.Lock()


def log(msg: str, level: str = "info") -> None:
    line = f"{dt.datetime.now():%H:%M:%S} {msg}"
    with _lock:
        LOG.append({"t": line, "level": level})
    logging.info(msg)
    print(line, flush=True)


def push(title: str, body: str = "") -> None:
    """Alerte visible : bandeau de l'interface + notification Windows + téléphone (ntfy) si configuré."""
    with _lock:
        ALERTS.append({"t": dt.datetime.now().strftime("%H:%M"), "title": title, "body": body})
    log(f"NOTIF {title} {body}", "notif")
    threading.Thread(target=_send, args=(title, body), daemon=True).start()


def _send(title, body):
    topic = (load_settings().get("ntfy_topic") or "").strip()
    if topic:
        try:
            req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                                         headers={"Title": title.encode("utf-8").decode("latin-1", "ignore")})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:  # noqa
            logging.info("ntfy: %s", e)
    if IS_WIN:
        t = title.replace("'", "’")
        b = body.replace("'", "’")[:240]
        ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
              "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
              f"$x.GetElementsByTagName('text')[0].AppendChild($x.CreateTextNode('{t}')) > $null;"
              f"$x.GetElementsByTagName('text')[1].AppendChild($x.CreateTextNode('{b}')) > $null;"
              "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
              "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('StageAuto').Show($n)")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15,
                           creationflags=0x08000000)
        except Exception:
            pass
