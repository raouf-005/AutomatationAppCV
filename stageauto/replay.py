"""Relecture rapide d'une méthode mémorisée, sans appel à l'IA.

Pour un site déjà réussi, l'application rejoue : boutons d'ouverture (« Candidature simplifiée »…), puis à chaque étape
remplissage automatique (réponses autorisées + réponses apprises), textes (@@LETTRE@@), bouton d'étape (« Suivant »,
« Vérifier »…) et enfin bouton d'envoi, avec vérification de la confirmation.
Au premier imprévu (question inconnue, bouton absent, erreur affichée), la main passe à l'agent sur la page en l'état.
"""
import asyncio
import re
import time

from . import db, forms, memory
from .config import load_settings
from .forms import nrm

GENERIC_NEXT = ["Suivant", "Continuer", "Next", "Continue", "Vérifier", "Review", "Étape suivante", "Enregistrer et continuer",
                "Save and Continue", "Continuer ma candidature"]
# essai sans méthode connue : seuls ces libellés exacts sont cliqués, et l'envoi seulement si un formulaire est affiché
GENERIC_OPEN = ["Candidature simplifiée", "Easy Apply", "Postuler", "Postuler maintenant", "Je postule", "Postuler à cette offre",
                "Candidater", "Apply", "Apply now", "Apply for this job", "Je suis intéressé(e)", "I'm interested"]
GENERIC_SUBMIT = ["Envoyer ma candidature", "Envoyer la candidature", "Soumettre ma candidature", "Soumettre la candidature",
                  "Déposer ma candidature", "Valider ma candidature", "Submit application", "Submit my application",
                  "Send application", "Postuler", "Je postule", "Soumettre", "Submit", "Envoyer"]


def find(items, names, roles=("button", "link"), exact=False):
    """Élément visible dont le nom correspond à un nom mémorisé (exact, puis préfixe sauf si exact=True)."""
    cands = [i for i in items if i["role"] in roles or (i["role"] == "button" and "button" in roles)]
    for n in names:
        k = nrm(n).strip()
        if not k:
            continue
        for i in cands:
            if nrm(i["name"]).strip() == k:
                return i
        if exact:  # sans méthode connue : le libellé seul, ou suivi de l'intitulé (« Candidature simplifiée pour Stage X »)
            pat = re.compile(re.escape(k) + r"( (pour|for|a|to|sur|chez) .*)?$")
            hit = next((i for i in cands if pat.match(nrm(i["name"]).strip())), None)
            if hit:
                return hit
            continue
        for i in cands:
            v = nrm(i["name"]).strip()
            if v.startswith(k) or (len(v) > 3 and k.startswith(v)):
                return i
    return None


def _fields(snap) -> int:
    return sum(1 for i in snap["items"] if i["role"].startswith(("textbox", "textarea", "select", "file", "radio", "checkbox")))


async def replay(browser, tab, rkey, files, offer, auto=True) -> dict | None:
    """Renvoie None (rien à tenter), ou {status, tab, reason, mode, seconds, learned}. status = fallback -> l'agent prend
    la suite. Trois niveaux : méthode du site (mode replay), méthode du site final ou de sa plateforme de recrutement
    prise en cours de route, et, sans aucune méthode, un essai avec les boutons courants (mode générique)."""
    r = (db.method(rkey) or {}).get("recipe")
    generic = not r or r.get("ok", 0) < 1
    if generic:
        if not load_settings().get("generic_replay", True):
            return None
        r = {"open": GENERIC_OPEN, "next": [], "submit": GENERIC_SUBMIT, "tokens": []}
    else:
        r = dict(r)
    t0 = time.time()
    learned = {"open": [], "next": [], "submit": [], "tokens": [], "uploads": []}
    progress = False
    ctx = {"cv": files.get("cv"), "lm": files.get("lm"), "certs": files.get("certs"), "lettre_txt": files.get("lettre_txt", ""),
           "source": offer.get("source", "Autre"), "no_semicolon": files.get("no_semicolon"), "region": offer.get("region")}

    def out(status, reason=""):
        return {"status": status, "tab": tab, "reason": reason, "mode": "générique" if generic else "replay",
                "seconds": round(time.time() - t0), "learned": learned if generic else None, "progress": progress}

    # 1. ouvrir le formulaire
    snap = await tab.snapshot()
    host0 = memory.host_of(snap.get("url"))
    if not snap.get("dialog") and _fields(snap) < 2:
        for name in r.get("open", []):
            it = find(snap["items"], [name], exact=generic)
            if not it:
                continue
            before = set(await browser.tab_ids())
            await tab.click(it["ref"])
            await asyncio.sleep(0.25)
            nt = await browser.wait_new_tab(before, timeout=1.0, opener=tab.id)
            if nt:
                tab = nt
            await tab.settle(8)
            snap = await tab.snapshot()
            learned["open"].append(re.split(r"\s+(?:pour|for|à|to)\s+|\s+[–-]\s+", it["name"], maxsplit=1)[0].strip()[:60])
            progress = True
            if snap.get("dialog") or _fields(snap) >= 2 or memory.host_of(snap.get("url")) != host0:
                break
    # site final différent du site de départ : mur de compte déjà connu ? méthode de ce site ou de sa plateforme ?
    host1 = memory.host_of(snap.get("url"))
    if host1 and host1 != host0:
        why = memory.wall(host1)
        if why:
            return out("needs_login", why)
        r2, origin = memory.recipe_for(host1)
        if r2:
            r["next"] = (r2.get("next") or []) + (r.get("next") or [])
            r["submit"] = (r2.get("submit") or []) + (r.get("submit") or [])
            r["tokens"] = (r2.get("tokens") or []) + (r.get("tokens") or [])
            for name in r2.get("open", []):  # bouton d'ouverture propre au site final (« Postuler » sur la page de l'offre)
                it = find(snap["items"], [name])
                if it and not snap.get("dialog") and _fields(snap) < 2:
                    await tab.click(it["ref"])
                    await tab.settle(8)
                    snap = await tab.snapshot()
                    break
    if generic and not snap.get("dialog") and _fields(snap) < 2:
        return out("fallback", "pas de formulaire reconnu sans IA")
    # 2. étapes
    last_errors = None
    for _ in range(15):
        snap = await tab.snapshot()
        if snap.get("captcha"):
            return out("needs_login", "captcha")
        if forms.looks_success(snap["url"], snap["text"]) and (progress or not generic):
            return out("applied", "confirmation vue")
        try:
            rep = await forms.auto_fill(tab, ctx, offer, snap=snap)
        except forms.NeedLogin:
            return out("fallback", "connexion demandée")
        except forms.Blocked as e:
            return out("blocked", str(e))
        progress = progress or bool(rep.get("filled"))
        snap = await tab.snapshot()
        for name, token in r.get("tokens", []):
            it = find(snap["items"], [name], roles=("textarea", "textbox:text", "textbox:editable"))
            if it and not it.get("value"):
                text = {"@@LETTRE@@": ctx["lettre_txt"], "@@MESSAGE@@": files.get("message_court", "")}.get(token, "")
                if ctx.get("no_semicolon"):
                    text = text.replace(";", ",")
                await tab.type(it["ref"], text)
        if rep["unanswered_required"]:
            return out("fallback", "questions sans réponse : " + "; ".join(rep["unanswered_required"][:3]))
        snap = await tab.snapshot()
        final = find(snap["items"], r.get("submit", []), roles=("button",), exact=generic)
        if final and generic and not (snap.get("dialog") or _fields(snap) >= 2):
            final = None  # sans méthode connue, jamais d'envoi hors d'un formulaire affiché
        if final:
            if not auto:
                return out("ready", "rempli, envoi en attente de validation")
            probs = forms.presubmit_problems(await tab.snapshot(keep=True))
            if probs:
                return out("fallback", "vérification avant envoi : " + " ; ".join(probs[:3]))
            await tab.click(final["ref"])
            learned["submit"].append(final["name"])
            for _ in range(24 if generic else 40):
                await asyncio.sleep(0.75)
                s2 = await tab.snapshot()
                if forms.looks_success(s2["url"], s2["text"]):
                    return out("applied", "confirmation vue")
                if s2.get("errors"):
                    return out("fallback", "erreur après envoi : " + "; ".join(s2["errors"][:2]))
            return out("fallback", "pas de confirmation après l'envoi")
        nxt = find(snap["items"], r.get("next", []) + GENERIC_NEXT, roles=("button",), exact=generic)
        if not nxt:
            return out("fallback", "aucun bouton connu sur cette étape")
        await tab.click(nxt["ref"])
        if nxt["name"] not in learned["next"]:
            learned["next"].append(nxt["name"])
        progress = True
        await tab.settle(6)
        s2 = await tab.snapshot()
        if s2.get("errors") and s2["errors"] == last_errors:
            return out("fallback", "étape refusée : " + "; ".join(s2["errors"][:2]))
        last_errors = s2.get("errors")
    return out("fallback", "trop d'étapes")
