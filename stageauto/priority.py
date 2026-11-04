"""Priorité des offres (postuler parmi les premiers) et pré-tri sans IA.

score : plus l'offre est récente et peu disputée, plus elle passe tôt :
- âge lu sur la carte ou la page (« il y a 12 minutes », « 3 hours ago », date de l'API) ;
- signaux « Soyez parmi les premiers candidats », « Recrutement actif », nombre de candidats ;
- candidature simplifiée et méthode déjà mémorisée pour le site (candidature plus rapide).

prescreen : écarte, sans appel à l'IA, les offres manifestement hors cible (alternance, CDI, moins de 6 mois,
habilitation ou nationalité française exigée). La date de début n'écarte jamais une offre : un stage de fin d'études de
6 mois est retenu quel que soit son démarrage. En cas de doute, l'offre passe : l'IA tranche.
"""
import datetime as dt
import re

from . import db
from .forms import nrm

_UNITS = {"min": 1, "minute": 1, "h": 60, "heure": 60, "hour": 60, "j": 1440, "jour": 1440, "day": 1440,
          "semaine": 10080, "week": 10080, "sem": 10080}
AGE_RES = [
    re.compile(r"il y a (\d+)\s*(min|minute|h|heure|j|jour|semaine|sem)s?\b"),
    re.compile(r"(\d+)\s*(min|minute|h|hour|day|week)s?\s+ago\b"),
    re.compile(r"(?:publiee?|posted|republiee?|reposted)\s+(?:il y a\s+)?(\d+)\s*(min|minute|h|heure|hour|j|jour|day)s?\b"),
]
EARLY_RE = re.compile(r"parmi les (\d+ )?premiers|soyez parmi|early applicant|be among the first|be an early|"
                      r"recrutement actif|actively (recruiting|hiring)", re.I)
APPLICANTS_RE = re.compile(r"(plus de |over )?(\d+)\s+(candidats?|applicants?|candidatures?)", re.I)


def age_minutes(text: str):
    t = nrm(text or "")
    if re.search(r"a l.instant|just now|maintenant|moments? ago|il y a quelques (secondes|minutes)", t):
        return 0
    best = None
    for rx in AGE_RES:
        for m in rx.finditer(t):
            unit = m.group(2)
            mult = next((v for k, v in _UNITS.items() if unit.startswith(k)), None)
            if mult is None:
                continue
            v = int(m.group(1)) * mult
            best = v if best is None else min(best, v)
    if best is not None:
        return best
    if re.search(r"aujourd.hui|today", t):
        return 360
    if re.search(r"\bhier\b|yesterday", t):
        return 1440
    return None


def age_from_iso(iso: str):
    try:
        when = dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return max(0, int((dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 60))


def applicants(text: str):
    m = APPLICANTS_RE.search(nrm(text or ""))
    if not m:
        return None
    n = int(m.group(2))
    return n + 1 if m.group(1) else n


_RECIPE_SITES = None


def _sites_with_recipe():
    global _RECIPE_SITES
    if _RECIPE_SITES is None:
        _RECIPE_SITES = {r["site"].split("|")[0] for r in db.q("SELECT site FROM methods WHERE site LIKE '%|%'")}
    return _RECIPE_SITES


def refresh_recipes():
    global _RECIPE_SITES
    _RECIPE_SITES = None


def score(o: dict) -> float:
    text = " ".join(str(o.get(k) or "") for k in ("card", "meta"))
    age = o.get("age_min")
    if age is None:
        age = age_from_iso(o.get("posted")) if o.get("posted") else age_minutes(text)
    o["age_min"] = age
    n = applicants(text)
    o["applicants"] = n
    s = 50.0
    if age is not None:
        s += max(-10.0, 40 - age / 15)          # 0 min : +40 ; 10 h : 0 ; plus vieux : jusqu'à -10
    if EARLY_RE.search(text):
        s += 20
    if n is not None:
        s += 20 - n / 2 if n < 40 else (-10 if n >= 100 else 0)
    if o.get("easy"):
        s += 10
    if o.get("site") in _sites_with_recipe():
        s += 8
    o["score"] = round(s, 1)
    return o["score"]


# ------------------------------------------------------------------------------------ pré-tri
NO_TITLE = re.compile(r"\b(alternance|alternant|apprenti|apprentissage|contrat pro|cdi|cdd|freelance|interim|v\.?i\.?e)\b")
SHORT = re.compile(r"(stage|duree|duration|internship|periode de stage)\s*(de|d.une duree de|:|of)?\s*([1-5])\s*(mois|months?)\b|"
                   r"\b([1-5])\s*mois de stage|\b([1-5])[- ]months? (internship|stage)")
SIX = re.compile(r"\b6\s*mois\b|six mois|\b6\s*months?\b|six months|fin d.etudes|end of studies|\bpfe\b|\b6 a 6\b|\b(4|5)\s*a\s*6\s*mois\b")
CLEARANCE = re.compile(r"habilitation (secret|confidentiel|defense|de securite)|habilitable|secret defense|"
                       r"(de )?nationalite francaise (exigee|requise|obligatoire|indispensable)|etre de nationalite francaise|"
                       r"citoyennete (francaise|europeenne|ue)|ressortissant (francais|de l.union)|security clearance|"
                       r"eu citizen|french citizen|nationality: french")


def prescreen(o: dict) -> str | None:
    """Raison d'écarter l'offre sans IA, ou None si elle doit être étudiée."""
    title = nrm(o.get("title", ""))
    desc = nrm(o.get("description", ""))
    if NO_TITLE.search(title) and not re.search(r"\bstage|stagiaire|intern", title):
        return "alternance / CDI / autre contrat (titre)"
    if re.search(r"\b(alternance|apprentissage)\b", title):
        return "alternance (titre)"
    if CLEARANCE.search(desc):
        return "habilitation ou nationalité française / UE exigée"
    if SHORT.search(desc) and not SIX.search(desc):
        return "durée inférieure à 6 mois"
    return None  # date de début : jamais un motif d'écart (règle de l'utilisateur)
