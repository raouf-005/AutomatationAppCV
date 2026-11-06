"""Règles de candidature modifiables dans l'onglet « Règles » du tableau de bord.

- Offres récentes seulement : publiées dans les dernières 24 heures (comme le filtre « 24 dernières heures » de LinkedIn).
- LinkedIn d'abord : les offres LinkedIn passent en tête pour la préparation des CV et les candidatures.
- Langue du CV : anglais si l'offre demande un CV en anglais ou est rédigée en anglais, sinon français.
- Règles libres : une par ligne, transmises à l'IA pour le CV, la lettre et les formulaires.
"""
import datetime as dt
import re

from .config import load_settings

OLD_RE = re.compile(r"\bhier\b|yesterday|il y a \d+\s*(j\b|jours?|semaines?|mois)|\d+\s*(days?|weeks?|months?) ago|"
                    r"publi[ée]e? le \d{1,2}[/.]\d{1,2}", re.I)
EN_ASKED = re.compile(r"(cv|resume|r[ée]sum[ée]|candidature|application|lettre de motivation|cover letter|motivation letter)s?"
                      r"[^.\n]{0,40}\b(en|in)\s+(anglais|english)\b|\b(english|anglais)\s+(cv|resume|r[ée]sum[ée])|"
                      r"(send|submit|upload|envoyez|transmettez)[^.\n]{0,40}\b(in english|en anglais)", re.I)
FR_WORDS = re.compile(r"\b(le|la|les|des|du|une|et|vous|nous|pour|avec|dans|sur|est|sont|votre|notre|au|aux|ce|cette)\b", re.I)
EN_WORDS = re.compile(r"\b(the|and|you|we|for|with|in|on|is|are|your|our|to|of|this|will|be|an)\b", re.I)


def _s():
    return load_settings()


def only_today() -> bool:
    return bool(_s().get("rule_only_today", True))


def linkedin_first() -> bool:
    return bool(_s().get("rule_linkedin_first", True))


def window(hours: float) -> float:
    """Fenêtre de recherche : 24 heures au plus (comme le filtre « 24 dernières heures » de LinkedIn)."""
    return min(hours, 24.25) if only_today() else hours


def is_today(o: dict) -> bool:
    """Offre publiée dans les dernières 24 h ? Date fournie par le site, sinon âge lu sur la carte, sinon trouvée
    il y a moins de 24 h. LinkedIn et HelloWork sont déjà filtrés par leur recherche (24 dernières heures)."""
    now = dt.datetime.now()
    posted = o.get("posted")
    if posted:
        try:
            d = dt.datetime.fromisoformat(str(posted).replace("Z", "+00:00"))
            if d.tzinfo:
                d = d.astimezone().replace(tzinfo=None)
            return now - d <= dt.timedelta(hours=24.5)
        except ValueError:
            pass
    from .priority import age_minutes
    age = o.get("age_min")
    if age is None:
        age = age_minutes(" ".join(str(o.get(k) or "") for k in ("card", "meta")))
    if age is not None:
        return age <= 1500
    if o.get("site") in ("linkedin", "hellowork"):
        return True
    if OLD_RE.search(" ".join(str(o.get(k) or "") for k in ("card", "meta"))) and \
            not re.search(r"\bhier\b|yesterday|il y a 1 j|1 day ago", " ".join(str(o.get(k) or "") for k in ("card", "meta")), re.I):
        return False
    found = str(o.get("found_at") or "")
    try:
        return not found or now - dt.datetime.fromisoformat(found) <= dt.timedelta(hours=24.5)
    except ValueError:
        return True


def site_order(ids: list) -> list:
    """LinkedIn d'abord, puis HelloWork, puis les autres sites actifs, dans l'ordre du réglage site_order."""
    pref = _s().get("site_order") or ["linkedin", "hellowork"]
    return sorted(ids, key=lambda i: pref.index(i) if i in pref else len(pref))


def keep_today(offers: list) -> list:
    return [o for o in offers if is_today(o)] if only_today() else offers


def order(offers: list) -> list:
    """LinkedIn d'abord (si la règle est active), puis par priorité."""
    first = linkedin_first()
    return sorted(offers, key=lambda o: ((o.get("site") != "linkedin") if first else 0, -(o.get("score") or 0)))


def cv_lang(offer: dict) -> tuple[str, str]:
    """(fr|en, raison). Anglais si l'offre demande un CV en anglais ou est rédigée en anglais ; sinon français. Un
    « anglais courant exigé » est une compétence, pas une demande de CV en anglais."""
    if not _s().get("rule_cv_lang", True):
        return "", ""
    text = f"{offer.get('title') or ''}\n{offer.get('description') or ''}"
    if EN_ASKED.search(text):
        return "en", "l'offre demande un CV / une candidature en anglais"
    fr, en = len(FR_WORDS.findall(text)), len(EN_WORDS.findall(text))
    if en > 2 * max(fr, 5):
        return "en", "l'offre est rédigée en anglais"
    return "fr", "l'offre ne demande pas de CV en anglais"


def custom() -> list[str]:
    return [l.strip(" -•\t") for l in (_s().get("rules_custom") or "").splitlines() if l.strip(" -•\t")]


def prompt_block(offer: dict | None = None) -> str:
    """Texte ajouté aux demandes faites à l'IA (CV, lettre, formulaires)."""
    out = []
    if offer is not None:
        lang, why = cv_lang(offer)
        if lang:
            out.append(f"LANGUE IMPOSÉE du CV et de la lettre : {'ANGLAIS' if lang == 'en' else 'FRANÇAIS'} ({why}). "
                       "Champ lang = " + lang + ".")
    rules = custom()
    if rules:
        out.append("RÈGLES DE L'UTILISATEUR (à respecter, sans jamais enfreindre les interdits ni inventer) :\n" +
                   "\n".join(f"- {r}" for r in rules))
    return "\n\n".join(out)
