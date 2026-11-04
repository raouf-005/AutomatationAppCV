"""Identifiants stockés dans le Gestionnaire d'identification Windows (keyring), jamais en clair sur le disque."""
import json

try:
    import keyring
except Exception:  # pragma: no cover
    keyring = None

SERVICE = "StageAuto"
_mem: dict = {}


def _get(name):
    if keyring:
        try:
            return keyring.get_password(SERVICE, name)
        except Exception:
            pass
    return _mem.get(name)


def _set(name, value):
    if keyring:
        try:
            if value is None:
                keyring.delete_password(SERVICE, name)
            else:
                keyring.set_password(SERVICE, name, value)
            return
        except Exception:
            pass
    if value is None:
        _mem.pop(name, None)
    else:
        _mem[name] = value


def get_api_key():
    return _get("anthropic_api_key")


def set_api_key(v):
    _set("anthropic_api_key", v or None)


def get_cred(site: str):
    raw = _get("cred:" + site)
    return json.loads(raw) if raw else None


def set_cred(site: str, user: str, password: str):
    if not user and not password:
        _set("cred:" + site, None)
    else:
        _set("cred:" + site, json.dumps({"user": user, "password": password}))


def sites_with_creds(sites):
    return {s: bool(get_cred(s)) for s in sites}


# Domaines reconnus pour chaque site (un export Proton Pass contient l'URL de chaque identifiant).
SITE_HOSTS = {
    "linkedin": ["linkedin.com"], "jobteaser": ["jobteaser.com"], "wttj": ["welcometothejungle.com"],
    "indeed": ["indeed.com", "indeed.fr"], "hellowork": ["hellowork.com"], "orange": ["orange.jobs"],
    "icims_orange": ["icims.com"], "gmail": ["google.com", "gmail.com"],
}


def import_proton_csv(path: str, delete_after: bool = True) -> dict:
    """Importe un export CSV Proton Pass (colonnes name, url, email/username, password).
    Seuls les identifiants des sites d'emploi sont gardés ; le fichier est supprimé ensuite."""
    import csv
    import os
    from urllib.parse import urlparse

    done, other = {}, 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            urls = (row.get("url") or row.get("urls") or row.get("website") or "").replace(",", " ").split()
            user = row.get("username") or row.get("email") or row.get("login") or ""
            pwd = row.get("password") or ""
            if not pwd or not urls:
                continue
            host = ""
            for u in urls:
                host = (urlparse(u if "://" in u else "https://" + u).hostname or "").lower()
                if host:
                    break
            site = next((s for s, hs in SITE_HOSTS.items() if any(host.endswith(h) for h in hs)), None)
            if site is None:
                # autres sites carrières (ATS) : clé = domaine
                if any(k in host for k in ("career", "carriere", "recrut", "jobs", "talent", "smartrecruiters",
                                           "teamtailor", "taleez", "workday", "icims", "lever", "greenhouse")):
                    site = "host:" + host
                else:
                    other += 1
                    continue
            set_cred(site, user, pwd)
            done[site] = user
    if delete_after:
        try:
            os.remove(path)
        except OSError:
            pass
    return {"imported": done, "ignored": other}


def get_foreign_key():
    return _get("foreign_api_key")


def set_foreign_key(v):
    _set("foreign_api_key", v or None)


def get_provider_key(pid: str):
    """Clé API d'un fournisseur d'IA (ChatGPT, Gemini, Kimi, Qwen…), dans le Gestionnaire d'identification Windows."""
    if pid == "anthropic":
        return get_api_key()
    return _get("key:" + pid)


def set_provider_key(pid: str, v):
    if pid == "anthropic":
        set_api_key(v)
    else:
        _set("key:" + pid, v or None)
