"""Règles de candidature modifiables dans l'onglet « Règles » du tableau de bord.

- Offres du jour seulement : publiées aujourd'hui (date de l'offre, ou « il y a 3 h », sinon trouvée aujourd'hui).
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
    """Fenêtre de recherche limitée à aujourd'hui (depuis minuit, un quart d'heure de marge)."""
    if not only_today():
        return hours
    now = dt.datetime.now()
    since_midnight = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600
    return max(0.5, min(hours, since_midnight + 0.25))


def is_today(o: dict) -> bool:
    """Offre publiée aujourd'hui ? Date fournie par le site, sinon âge lu sur la carte, sinon trouvée aujourd'hui."""
    today = dt.date.today()
    posted = o.get("posted")
    if posted:
        try:
            d = dt.datetime.fromisoformat(str(posted).replace("Z", "+00:00"))
            if d.tzinfo:
                d = d.astimezone()
            return d.date() == today
        except ValueError:
            pass
    from .priority import age_minutes
    age = o.get("age_min")
    if age is None:
        age = age_minutes(" ".join(str(o.get(k) or "") for k in ("card", "meta")))
    if age is not None:
        return dt.datetime.now() - dt.timedelta(minutes=age) >= dt.datetime.combine(today, dt.time())
    if OLD_RE.search(" ".join(str(o.get(k) or "") for k in ("card", "meta"))):
        return False
    found = str(o.get("found_at") or "")[:10]
    return not found or found == today.isoformat()


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
