"""Mémoire des sites : aller plus vite à chaque candidature.

- Méthode par site ET par plateforme de recrutement : une candidature réussie sur le Greenhouse d'une entreprise sert
  aussitôt pour toutes les autres entreprises qui utilisent Greenhouse (mêmes boutons, mêmes étapes).
- Méthode du site final : quand « Postuler » mène du site d'emploi au site de l'entreprise, la méthode est gardée sous
  le nom de ce site final, pas mélangée avec celles des autres entreprises.
- Mur de compte mémorisé : un site qui a exigé un compte une fois est reconnu dès l'arrivée, sans appeler l'IA ; l'offre
  va tout de suite dans « À postuler toi-même ».
"""
import datetime as dt
import re

from . import db, vault

ATS = [("workday", r"myworkdayjobs\.com|myworkdaysite\.com"), ("greenhouse", r"greenhouse\.io"), ("lever", r"lever\.co"),
       ("smartrecruiters", r"smartrecruiters\.com"), ("teamtailor", r"teamtailor\.com"), ("recruitee", r"recruitee\.com"),
       ("taleo", r"taleo\.net"), ("successfactors", r"successfactors\.|sapsf\."), ("icims", r"icims\.com"),
       ("jobvite", r"jobvite\.com"), ("workable", r"workable\.com"), ("ashby", r"ashbyhq\.com"), ("personio", r"personio\."),
       ("breezy", r"breezy\.hr"), ("bamboohr", r"bamboohr\.com"), ("talentsoft", r"talent-soft\.com|talentsoft\."),
       ("flatchr", r"flatchr\.io"), ("digitalrecruiters", r"digitalrecruiters\.com"), ("jobaffinity", r"jobaffinity\.fr"),
       ("welcomekit", r"welcomekit\.co"), ("oracle", r"oraclecloud\.com"), ("avature", r"avature\.net"),
       ("beetween", r"beetween\.com"), ("eightfold", r"eightfold\.ai"), ("cornerstone", r"csod\.com")]
BOARDS = ("linkedin.com", "hellowork.com", "indeed.", "jobteaser.com", "welcometothejungle.com")
WALL_DAYS = 45  # un mur de compte est recontrôlé après ce délai (le site a pu changer)


def host_of(url: str) -> str:
    return re.sub(r"^https?://([^/:?#]+).*", r"\1", url or "").lower().removeprefix("www.")


def family(host: str) -> str:
    return next((name for name, pat in ATS if re.search(pat, host or "")), "")


def is_board(host: str) -> bool:
    return any(b in (host or "") for b in BOARDS)


def ext_key(host: str) -> str:
    return f"ext|{host}"


def fam_key(host: str) -> str:
    f = family(host)
    return f"ats|{f}" if f else ""


def recipe_for(host: str):
    """(méthode, origine) du site final : la sienne, sinon celle de sa plateforme de recrutement."""
    for key, origin in ((ext_key(host), "site"), (fam_key(host), "plateforme")):
        r = (db.method(key) or {}).get("recipe") if key else None
        if r and r.get("ok", 0) >= 1:
            return r, origin
    return None, ""


def _merge(old: list, new: list) -> list:
    out = list(old or [])
    for v in new or []:
        if v and v not in out:
            out.append(v)
    return out[:8]


def save(key: str, parts: dict) -> None:
    m = db.method(key)
    r = m.get("recipe") or {"open": [], "next": [], "submit": [], "tokens": [], "uploads": [], "ok": 0}
    for k in ("open", "next", "submit", "tokens", "uploads"):
        r[k] = _merge(r.get(k), parts.get(k))
    r["ok"] = r.get("ok", 0) + 1
    r["updated"] = db.now()
    m["recipe"] = r
    db.save_method(key, m)


def learn_success(rkey: str, start_url: str, final_url: str, learned: dict | None = None) -> list:
    """Après une candidature envoyée : la méthode est rangée sous le site de départ (si elle vient d'un essai sans
    méthode), sous le site final et sous sa plateforme. Renvoie les noms mémorisés (pour le journal)."""
    saved = []
    parts = learned or (db.method(rkey) or {}).get("recipe")
    if not parts:
        return saved
    if learned:
        save(rkey, learned)
        saved.append(rkey)
    h0, h1 = host_of(start_url), host_of(final_url)
    if h1 and not is_board(h1):
        ext = dict(parts)
        if h1 != h0:  # les boutons d'ouverture appartiennent au site de départ
            ext = {**parts, "open": []}
        for key in (ext_key(h1), fam_key(h1)):
            if key:
                save(key, ext)
                saved.append(key)
        if wall(h1):
            db.set_host(h1, "ok", "candidature réussie")
    return saved


def _row(host: str):
    r = db.q("SELECT verdict, note, updated FROM hosts WHERE host=?", (host,))
    return r[0] if r else None


def wall(host: str) -> str:
    """Raison si ce site (ou sa plateforme) a déjà exigé un compte récemment, sinon ''. Ignoré si des identifiants
    sont enregistrés pour ce site : l'agent pourra s'y connecter."""
    if not host or is_board(host):
        return ""
    try:
        if vault.get_cred("host:" + host):
            return ""
    except Exception:
        pass
    limit = (dt.datetime.now() - dt.timedelta(days=WALL_DAYS)).isoformat()
    for h in (host, fam_key(host)):
        r = _row(h) if h else None
        if r and r["verdict"] == "account" and (r["updated"] or "") >= limit:
            return f"compte demandé sur {host} (mémorisé le {(r['updated'] or '')[:10]})"
    return ""


def note_account(url: str, why: str = "") -> None:
    """Ce site a exigé une connexion ou un compte. Deux sites d'une même plateforme sans aucune réussite : toute la
    plateforme est considérée « compte requis » (Workday, Taleo…)."""
    host = host_of(url)
    if not host or is_board(host):
        return
    db.set_host(host, "account", why[:200])
    fk = fam_key(host)
    if fk and not (db.method(fk) or {}).get("recipe"):
        pat = dict(ATS)[family(host)]
        n = sum(1 for r in db.q("SELECT host FROM hosts WHERE verdict='account'") if re.search(pat, r["host"]))
        if n >= 2:
            db.set_host(fk, "account", f"{n} sites de cette plateforme ont exigé un compte")


def forget(host: str) -> None:
    db.x("DELETE FROM hosts WHERE host=?", (host,))


def overview() -> dict:
    """Pour l'onglet Mémoire : sites et plateformes mémorisés, murs de compte connus."""
    sites = []
    for r in db.q("SELECT site, data FROM methods"):
        import json
        rec = (json.loads(r["data"] or "{}") or {}).get("recipe")
        if rec and rec.get("ok"):
            kind = {"ext": "site d'entreprise", "ats": "plateforme"}.get(r["site"].split("|")[0], "site d'emploi")
            sites.append({"key": r["site"], "kind": kind, "ok": rec.get("ok", 0), "fallbacks": rec.get("fallbacks", 0),
                          "updated": (rec.get("updated") or "")[:10]})
    walls = [{"host": r["host"], "note": r["note"], "updated": (r["updated"] or "")[:10]}
             for r in db.q("SELECT host, note, updated FROM hosts WHERE verdict='account' ORDER BY updated DESC")]
    return {"sites": sorted(sites, key=lambda s: -s["ok"]), "walls": walls}
