"""Relecture rapide d'une méthode mémorisée, sans appel à l'IA.

Pour un site déjà réussi, l'application rejoue : boutons d'ouverture (« Candidature simplifiée »…), puis à chaque étape
remplissage automatique (réponses autorisées + réponses apprises), textes (@@LETTRE@@), bouton d'étape (« Suivant »,
« Vérifier »…) et enfin bouton d'envoi, avec vérification de la confirmation.
Au premier imprévu (question inconnue, bouton absent, erreur affichée), la main passe à l'agent sur la page en l'état.
"""
import asyncio
import time

from . import db, forms
from .forms import nrm

GENERIC_NEXT = ["Suivant", "Continuer", "Next", "Continue", "Vérifier", "Review", "Étape suivante", "Enregistrer et continuer",
                "Save and Continue", "Continuer ma candidature"]


def find(items, names, roles=("button", "link")):
    """Élément visible dont le nom correspond à un nom mémorisé (exact, puis préfixe)."""
    cands = [i for i in items if i["role"] in roles or (i["role"] == "button" and "button" in roles)]
    for n in names:
        k = nrm(n).strip()
        if not k:
            continue
        for i in cands:
            if nrm(i["name"]).strip() == k:
                return i
        for i in cands:
            v = nrm(i["name"]).strip()
            if v.startswith(k) or (len(v) > 3 and k.startswith(v)):
                return i
    return None


async def replay(browser, tab, rkey, files, offer, auto=True) -> dict | None:
    """Renvoie None (aucune méthode), ou {status, tab, reason, mode, seconds}. status = fallback -> l'agent prend la suite."""
    r = (db.method(rkey) or {}).get("recipe")
    if not r or r.get("ok", 0) < 1:
        return None
    t0 = time.time()
    ctx = {"cv": files.get("cv"), "lm": files.get("lm"), "certs": files.get("certs"), "lettre_txt": files.get("lettre_txt", ""),
           "source": offer.get("source", "Autre"), "no_semicolon": files.get("no_semicolon"), "region": offer.get("region")}

    def out(status, reason=""):
        return {"status": status, "tab": tab, "reason": reason, "mode": "replay", "seconds": round(time.time() - t0)}

    # 1. ouvrir le formulaire
    snap = await tab.snapshot()
    fields = [i for i in snap["items"] if i["role"].startswith(("textbox", "textarea", "select", "file", "radio", "checkbox"))]
    if not snap.get("dialog") and len(fields) < 2:
        for name in r.get("open", []):
            it = find(snap["items"], [name])
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
    # 2. étapes
    last_errors = None
    for _ in range(15):
        snap = await tab.snapshot()
        if snap.get("captcha"):
            return out("needs_login", "captcha")
        if forms.looks_success(snap["url"], snap["text"]):
            return out("applied", "confirmation vue")
        try:
            rep = await forms.auto_fill(tab, ctx, offer, snap=snap)
        except forms.NeedLogin:
            return out("fallback", "connexion demandée")
        except forms.Blocked as e:
            return out("blocked", str(e))
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
        final = find(snap["items"], r.get("submit", []), roles=("button",))
        if final:
            if not auto:
                return out("ready", "rempli, envoi en attente de validation")
            probs = forms.presubmit_problems(await tab.snapshot(keep=True))
            if probs:
                return out("fallback", "vérification avant envoi : " + " ; ".join(probs[:3]))
            await tab.click(final["ref"])
            for _ in range(40):
                await asyncio.sleep(0.75)
                s2 = await tab.snapshot()
                if forms.looks_success(s2["url"], s2["text"]):
                    return out("applied", "confirmation vue")
                if s2.get("errors"):
                    return out("fallback", "erreur après envoi : " + "; ".join(s2["errors"][:2]))
            return out("fallback", "pas de confirmation après l'envoi")
        nxt = find(snap["items"], r.get("next", []) + GENERIC_NEXT, roles=("button",))
        if not nxt:
            return out("fallback", "aucun bouton connu sur cette étape")
        await tab.click(nxt["ref"])
        await tab.settle(6)
        s2 = await tab.snapshot()
        if s2.get("errors") and s2["errors"] == last_errors:
            return out("fallback", "étape refusée : " + "; ".join(s2["errors"][:2]))
        last_errors = s2.get("errors")
    return out("fallback", "trop d'étapes")
