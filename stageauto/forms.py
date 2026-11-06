"""Remplissage générique de formulaires de candidature (tous sites et ATS).

1. lit tous les champs visibles (libellé, type, options, obligatoire) en un seul appel JS ;
2. répond par règles (profil autorisé), puis par les réponses déjà apprises, puis par Claude (réponse mémorisée) ;
3. abandonne si une donnée sensible non autorisée est demandée (titre de séjour, casier, identité, banque, vidéo…).
"""
import asyncio
import re
import unicodedata

from . import db, llm
from .config import BLOCK_PATTERNS, PROFILE, load_settings
from .notify import log


BLOCK_RE = re.compile("|".join(BLOCK_PATTERNS), re.I)


class Blocked(Exception):
    """Offre à abandonner (donnée non autorisée, compte, vidéo, captcha)."""


class NeedLogin(Exception):
    """Le site demande une connexion : l'offre est mise de côté puis réessayée après connexion."""


def nrm(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def pick(options, *wanted):
    """Option dont le libellé correspond au mieux à l'une des réponses voulues."""
    opts = [(o, nrm(o)) for o in options if o and not re.match(r"^\s*(--|choisi|select|s[ée]lectionn)", nrm(o))]
    for w in wanted:
        w = nrm(w)
        for o, n in opts:
            if n == w:
                return o
        for o, n in opts:
            if w and (n.startswith(w) or w in n):
                return o
    return None


def is_yes_no(options):
    s = {nrm(o) for o in options}
    return bool(s & {"oui", "yes"}) and bool(s & {"non", "no"})


EDU_SECTION_RE = (r"\b(formations?|education|etudes?|diplomes?|degrees?|schools?|ecoles?|universit\w*|experiences?|emplois?|"
                  r"employment|work history|parcours|cursus)\b")


def rule_answer(label: str, f: dict, ctx: dict):
    """Réponse par règle. Renvoie (valeur, 'texte'|'option'|'check'|'file'|'skip') ou None. Lève Blocked."""
    L = nrm(label)
    P = {**PROFILE, **{nrm(k): v for k, v in load_settings().get("custom_answers", {}).items()}}
    opts = f.get("options") or []
    t = f["type"]
    # réponses personnalisées ajoutées dans l'interface (priorité)
    for k, v in load_settings().get("custom_answers", {}).items():
        if nrm(k) and nrm(k) in L:
            return (pick(opts, v) or v, "option" if opts else "texte")
    if t == "file":
        if re.search(r"lettre|motivation|cover", L):
            return (ctx.get("lm"), "file")
        if re.search(r"photo|image|avatar", L):
            return (None, "skip")
        if re.search(r"certif|autre|additional|other|document|pi[eè]ce", L) and ctx.get("cv_done"):
            return (ctx.get("certs"), "file") if ctx.get("certs") else (None, "skip")
        return (ctx.get("cv"), "file")
    if t == "password":
        raise NeedLogin("mot de passe demandé")
    # partie « Formation » ou « Expérience » (titre du bloc) : l'agent remplit chaque entrée avec la liste etudes du profil.
    # Une règle y mettrait la disponibilité dans « Date de début », ou Paris-Saclay dans l'établissement de la licence.
    if t not in ("checkbox", "radio") and re.search(EDU_SECTION_RE, nrm(f.get("section", ""))):
        return (None, "agent")  # ni règle, ni mémoire, ni question isolée à l'IA : l'agent voit tout le bloc
    if re.search(r"sponsor|parrainage|visa", L):
        return (pick(opts, "non", "no") or "Non", "option" if opts else "texte")
    for pat in BLOCK_PATTERNS:
        if re.search(pat, L):
            if re.search(r"vid[ée]o", L) and not f.get("required"):
                return (None, "skip")
            raise Blocked(f"donnée non autorisée demandée : {label[:80]}")
    if t == "checkbox":
        if re.search(r"newsletter|offres? similaires|marketing|partenaires|alerte|sms|promotion|job alert|^suivre|follow ", L):
            return ("", "check")
        if re.search(r"accept|consent|j'ai lu|conditions|confidentialit|privacy|rgpd|donn[ée]es|terms|politique|certifie|atteste|agree|cgu", L) or f.get("required"):
            return ("on", "check")
        return None
    rules = [
        (r"civilit|salutation|^titre\s*\*?$|^title\s*\*?$|appellation", ["Monsieur", "M.", "Mr", "M"]),
        (r"genre|gender|sexe", ["Homme", "Masculin", "Male", "Man", "M"]),
        (r"pr[ée]nom|first ?name|given name", [P["prenom"]]),
        (r"nom complet|full name|nom et pr[ée]nom|your name|^nom prenom", [P["nom_complet"]]),
        (r"^nom\b|last ?name|surname|family name|nom de famille|^name$", [P["nom"]]),
        (r"e-?mail|courriel", [P["email"]]),
        (r"indicatif|country code|code pays", ["+33", "France (+33)", "France"]),
        (r"t[ée]l[ée]phone|phone|mobile|portable", [P["tel"]]),
        (r"double nationalit", ["Non", "No", "Aucune"]),
        (r"nationalit", [P["nationalite"], P["nationalite_en"], "Algérie", "Algeria"]),
        (r"linkedin", [P["linkedin"]]),
        (r"github", [P["github"]]),
        (r"site web|website|portfolio|url", [P["linkedin"]]),
        (r"code postal|postal|zip|cp\b", [P["code_postal"]] if P.get("code_postal") else None),
        (r"ville|city|localit|lieu de r[ée]sidence|adresse|address|location|localisation|o[uù] habitez", [P["ville_longue"], P["ville"]]),
        (r"r[ée]gion|zone de recherche", [ctx.get("region") or P["region"], "Île-de-France"]),
        (r"pays|country", [P["pays"]]),
        (r"handicap|rqth|disabilit|travailleur handicap", [P["handicap"], "Non", "No", "Je ne suis pas"]),
        (r"am[ée]nagement|accommodat|adjustment", ["Non", "No"]),
        (r"d[ée]m[ée]nag|relocat|mobilit", ["Oui", "Yes"]),
        (r"date de d[ée]but|start date|disponib|availability|quand pouvez|d[ée]but souhait", [P["disponibilite"], P["disponibilite_en"]]),
        (r"dur[ée]e", ["6 mois", "6 months", "6"]),
        (r"niveau d'[ée]tude|niveau d.etudes|education level|degree|dipl[oô]me|level of education|niveau de formation",
         ["Master (Bac+5, en cours)", "Bac+5", "Master", "Master's degree", "Bac + 5"]),
        (r"[ée]cole|universit|[ée]tablissement|school|institution", [P["ecole"], "Autre", "Other"]),
        (r"type de stage|internship type", ["Stage de fin d'études", "End of studies", "Fin d'études"]),
        (r"anglais|english", ["Courant / bilingue (C2)", "C2", "Courant", "Bilingue", "Fluent", "Native"]),
        (r"fran[çc]ais|french", ["Natif", "Langue maternelle", "Courant / bilingue (C2)", "Courant", "Native", "Fluent"]),
        (r"pr[ée]tention|salaire|salary|r[ée]mun[ée]ration|gratification", [P["pretentions"]]),
        (r"ann[ée]es? d.exp|years of exp|exp[ée]rience \(ann", [P["experience_annees"]]),
        (r"comment avez|how did you hear|source|o[uù] avez-vous|connaissance de|origine", [ctx.get("source") or "Autre", "Autre", "Other", "LinkedIn"]),
        (r"lettre de motivation|motivation|cover letter|message|pourquoi|why|pr[ée]sentez|commentaire|additional information|informations compl",
         ["@@LETTRE@@"]),
    ]
    for pat, vals in rules:
        if re.search(pat, L):
            if vals is None:
                return (None, "skip")
            if vals == ["@@LETTRE@@"]:
                if f["tag"] == "textarea" or f["type"] in ("textarea", "text"):
                    txt = ctx.get("lettre_txt") or ""
                    if ctx.get("no_semicolon"):
                        txt = txt.replace(";", ",")
                    return (txt, "texte")
                return None
            if opts:
                o = pick(opts, *vals)
                if o:
                    return (o, "option")
                if is_yes_no(opts) and re.search(r"handicap|am[ée]nagement|rqth", L):
                    return (pick(opts, "non", "no"), "option")
                continue
            if f["type"] == "date":
                return (PROFILE["date_debut_iso"], "texte")
            v = vals[0]
            if f["type"] == "tel" and ctx.get("intl_phone"):
                v = P["tel_intl"]
            if f["type"] == "number" and not re.fullmatch(r"\d+", v):
                return (None, "skip")
            return (v, "texte")
    return None


def to_fields(items):
    """Éléments du snapshot -> champs de formulaire (les radios sont regroupés par question)."""
    fields, radios = [], {}
    for it in items:
        role = it["role"]
        if role.startswith("textbox"):
            ty = role.split(":", 1)[1] if ":" in role else "text"
            fields.append({"ref": it["ref"], "tag": "input", "type": ty if ty != "editable" else "textarea",
                           "label": it["name"], "value": it.get("value", ""), "required": it.get("required"), "options": [], "section": it.get("section", "")})
        elif role == "textarea":
            fields.append({"ref": it["ref"], "tag": "textarea", "type": "textarea", "label": it["name"],
                           "value": it.get("value", ""), "required": it.get("required"), "options": [], "section": it.get("section", "")})
        elif role == "select":
            fields.append({"ref": it["ref"], "tag": "select", "type": "select", "label": it["name"],
                           "value": it.get("value", ""), "required": it.get("required"), "options": it.get("options", []), "section": it.get("section", "")})
        elif role == "checkbox":
            fields.append({"ref": it["ref"], "tag": "input", "type": "checkbox", "label": it["name"],
                           "value": "on" if it.get("checked") else "", "required": it.get("required"), "options": []})
        elif role == "file":
            fields.append({"ref": it["ref"], "tag": "input", "type": "file", "label": it["name"],
                           "value": "x" if it.get("files") else "", "required": it.get("required"), "options": [],
                           "accept": it.get("accept", "")})
        elif role == "combobox":
            fields.append({"ref": it["ref"], "tag": "input", "type": "combobox", "label": it["name"],
                           "value": it.get("value", ""), "required": it.get("required"), "options": [], "section": it.get("section", "")})
        elif role == "radio":
            g = it.get("group") or it["name"]
            if g not in radios:
                radios[g] = {"ref": None, "tag": "radio", "type": "radio", "label": g, "value": "", "required": it.get("required"),
                             "options": [], "refs": []}
                fields.append(radios[g])
            radios[g]["options"].append(it["name"])
            radios[g]["refs"].append(it["ref"])
            if it.get("checked"):
                radios[g]["value"] = it["name"]
    return fields


async def auto_fill(tab, ctx: dict, offer: dict, use_llm=True, snap=None) -> dict:
    """Remplit d'un coup tous les champs reconnus de l'étape visible (règles -> réponses apprises -> Claude)."""
    snap = snap or await tab.snapshot()
    if snap.get("captcha"):
        raise Blocked("captcha")
    rep = {"filled": [], "llm": 0, "unanswered_required": [], "skipped": []}
    ctx = dict(ctx)
    combos = []
    for f in to_fields(snap["items"]):
        label = f["label"]
        if f["value"] and f["type"] not in ("checkbox",) and f["tag"] != "select":
            if f["type"] == "file" and re.search(r"cv|resume|curriculum", nrm(label)):
                ctx["cv_done"] = True
            continue
        ans = rule_answer(label, f, ctx)
        if ans is None and f["type"] not in ("checkbox", "file"):
            cached = db.get_answer(label, f.get("options"))
            if cached is not None:
                ans = (cached, "option" if f.get("options") else "texte")
            elif use_llm and label and (f.get("required") or f.get("options")):
                a = await asyncio.to_thread(llm.answer_question, label, f.get("options"), offer, f["type"])
                if a and a.get("sensitive"):
                    raise Blocked(f"question sensible : {label[:80]}")
                if a and a.get("answer"):
                    val = pick(f["options"], a["answer"]) if f.get("options") else a["answer"]
                    if val:
                        db.put_answer(label, f.get("options"), val)
                        ans = (val, "option" if f.get("options") else "texte")
                        rep["llm"] += 1
        if ans is None or ans[1] == "skip" or ans[0] is None:
            if f.get("required") and not f["value"]:
                rep["unanswered_required"].append(label[:90])
            continue
        value, how = ans
        try:
            if f["type"] == "radio":
                i = f["options"].index(value) if value in f["options"] else None
                if i is None:
                    o = pick(f["options"], value)
                    i = f["options"].index(o) if o else None
                if i is None:
                    rep["unanswered_required"].append(label[:90])
                    continue
                await tab.check(f["refs"][i], True)
            elif f["type"] == "checkbox":
                await tab.check(f["ref"], bool(value))
            elif f["type"] == "file":
                await tab.upload(f["ref"], value)
                if value == ctx.get("cv"):
                    ctx["cv_done"] = True
            elif f["tag"] == "select":
                if f["value"] and nrm(f["value"]) == nrm(value):
                    continue
                r = await tab.select(f["ref"], value)
                if r and r.get("error"):
                    rep["skipped"].append(f"{label[:40]} : {r['error']}")
                    continue
            elif f["type"] == "combobox":
                combos.append((label, value))
                continue
            else:
                await tab.type(f["ref"], value)
            rep["filled"].append(f"{label[:50]} = {str(value)[:40] if how != 'file' else value.split(chr(92))[-1].split('/')[-1]}")
        except Exception as e:
            rep["skipped"].append(f"{label[:50]} ({e})"[:120])
    for label, value in combos:  # listes à saisie (autocomplétion) : une à une, avec un snapshot frais
        try:
            s2 = await tab.snapshot()
            it = next((i for i in s2["items"] if i["role"] == "combobox" and i["name"] == label), None)
            if not it:
                continue
            await tab.type(it["ref"], value.split(",")[0][:40])
            await asyncio.sleep(1.3)
            s3 = await tab.snapshot()
            opt = next((i for i in s3["items"] if i["role"] == "option" and nrm(value.split(",")[0]) in nrm(i["name"])), None)
            if opt:
                await tab.click(opt["ref"])
            else:
                await tab.press("ArrowDown")
                await tab.press("Enter")
            rep["filled"].append(f"{label[:50]} = {value[:40]}")
        except Exception as e:
            rep["skipped"].append(f"{label[:50]} ({e})"[:120])
    if rep["unanswered_required"]:
        log("obligatoires sans réponse : " + "; ".join(rep["unanswered_required"][:4]))
    return rep


SUCCESS_RE = re.compile(
    r"candidature (a bien été|a été|est) (envoy|transmis|reçu|enregistr)|candidature envoy|merci pour votre candidature|"
    r"votre candidature a bien|nous avons bien reçu|application (was |has been )?(sent|submitted|received)|"
    r"thank you for (your )?appl|thanks for applying|bien été envoyée|candidature bien reçue|your application was sent", re.I)


def looks_success(url: str, text: str) -> bool:
    return bool(re.search(r"thank|merci|success|confirmation|bounce|applythankyou", url or "", re.I)) or \
        bool(SUCCESS_RE.search(text or ""))


PLACEHOLDER_RE = re.compile(r"^\s*(--|choisi|select|selectionn|veuillez|please|aucun|none|-)\b|^\s*$")


def presubmit_problems(snap) -> list:
    """Contrôle avant l'envoi : champs obligatoires vides, cases obligatoires, fichiers manquants, erreurs affichées."""
    probs, groups = [], {}
    for it in snap.get("items", []):
        role, name = it["role"], (it.get("name") or "")[:70]
        if role == "radio":
            g = groups.setdefault(it.get("group") or name, {"req": False, "checked": False})
            g["req"] |= bool(it.get("required"))
            g["checked"] |= bool(it.get("checked"))
            continue
        if it.get("invalid"):
            probs.append(f"champ signalé invalide : {name}")
            continue
        if not it.get("required"):
            continue
        if role.startswith("textbox") or role == "textarea":
            if not (it.get("value") or "").strip():
                probs.append(f"champ obligatoire vide : {name}")
        elif role == "select":
            if PLACEHOLDER_RE.search(nrm(it.get("value") or "")):
                probs.append(f"liste obligatoire non choisie : {name}")
        elif role == "checkbox":
            if not it.get("checked"):
                probs.append(f"case obligatoire non cochée : {name}")
        elif role == "file":
            if not it.get("files"):
                probs.append(f"fichier obligatoire manquant : {name}")
    probs += [f"question obligatoire sans réponse : {g[:70]}" for g, d in groups.items() if d["req"] and not d["checked"]]
    probs += [f"message d'erreur : {e}" for e in snap.get("errors", [])]
    return probs[:6]
