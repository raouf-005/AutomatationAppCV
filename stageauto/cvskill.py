"""CV + lettre avec le skill « candidature-stage-ciblee » d'Abderraouf (gabarit v16).

Un seul appel Claude par offre : éligibilité + CV .tex + lettre .tex + mots-clés ATS, en suivant SKILL.md et profil.md.
Puis build.py du skill (compilation, 1 page, ATS multi-moteurs, contenu actif) ; une correction groupée si erreurs.
Sans poppler/qpdf (Windows sans ces outils), contrôle allégé : compilation, 1 page, règles du skill, neutralisation pypdf.
Repli : générateur à modèles (gen/generator.py) si le skill échoue.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import llm, providers, rules
from .config import IS_WIN, SKILL_DIR, WORK, load_settings
from .gen import generator
from .notify import log

SKILL = SKILL_DIR

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["eligible", "reason", "lang", "company", "poste", "dossier", "cv_tex", "lettre_tex", "kw", "kw_opt",
                 "message_court", "ville", "recruteur"],
    "properties": {
        "eligible": {"type": "boolean", "description": "stage de fin d'études ou de 6 mois dans le domaine, exigences remplies (date de début indifférente)"},
        "reason": {"type": "string"},
        "lang": {"type": "string", "enum": ["fr", "en"]},
        "company": {"type": "string"},
        "poste": {"type": "string", "description": "intitulé court sans F/H ni référence"},
        "dossier": {"type": "string", "description": "<Entreprise>_<Sujet> (lettres, chiffres, _)"},
        "cv_tex": {"type": "string", "description": "CV LaTeX complet (vide si non éligible)"},
        "lettre_tex": {"type": "string", "description": "lettre LaTeX complète (vide si non éligible)"},
        "kw": {"type": "array", "items": {"type": "string"}, "description": "12 à 18 mots-clés bloquants écrits tels quels dans le CV"},
        "kw_opt": {"type": "array", "items": {"type": "string"}},
        "message_court": {"type": "string", "description": "message de candidature < 400 caractères, sans point-virgule"},
        "ville": {"type": "string"},
        "recruteur": {"type": "string", "description": "nom du recruteur ou manager s'il est cité, sinon vide"},
    },
}


def _system():
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    profil = (SKILL / "references" / "profil.md").read_text(encoding="utf-8")
    english = (SKILL / "references" / "english.md").read_text(encoding="utf-8")
    return ("Tu appliques le skill ci-dessous pour produire le CV et la lettre d'Abderraouf pour UNE offre. Ignore les "
            "instructions d'outils du skill (bash, SendUserFile) : rends directement les deux fichiers .tex complets, les "
            "mots-clés (kw = kw.txt, kw_opt = kw-opt.txt) et un message court. Vérifie d'abord l'éligibilité : stage de "
            "fin d'études ou de 6 mois (quelle que soit la date de début : 2026, 2027, dès que possible…) en cybersécurité, SOC/CERT, GRC, pentest, systèmes et réseaux, "
            "DevOps/DevSecOps, cloud/infra, automatisation, IA appliquée à la cyber ; non éligible si alternance, CDI, "
            "moins de 6 mois, autre domaine, nationalité française/citoyenneté UE/habilitation exigée, poste situé hors de "
            "France, ou exigence clé absente du profil (outils interdits du skill compris). Si non éligible : cv_tex et lettre_tex vides.\n"
            "Langue : la LANGUE IMPOSÉE indiquée avec l'offre l'emporte (anglais si l'offre demande un CV en anglais ou est "
            "rédigée en anglais, sinon français ; « anglais courant exigé » ne suffit pas) ; en anglais, suivre references/english.md.\n\n"
            f"===== SKILL.md =====\n{skill}\n\n===== references/profil.md =====\n{profil}\n\n"
            f"===== references/english.md =====\n{english}")


_SYS = None


def _conversation():
    global _SYS
    _SYS = _SYS or _system()
    return providers.JsonConversation(_SYS, SCHEMA, max_tokens=32000, effort=load_settings().get("cv_effort", "medium"),
                                      name="candidature", order=providers.providers_in_order(task="cv"))


RUNNER = Path(__file__).with_name("skillrun.py")
_BROKEN: dict = {}  # build.py complet planté une fois : contrôle allégé pour la suite de la session


def _tools_ok():
    return bool(generator.latex_bin()) and all(shutil.which(t) for t in ("pdftotext", "pdfinfo", "pdffonts", "qpdf"))


def _build_env():
    """Variables de LaTeX du générateur, dossier de pdflatex en tête du PATH (MiKTeX tout juste installé n'y est pas
    toujours), et Python en UTF-8."""
    env = generator._env()
    tex = generator.latex_bin()
    if tex:
        env["PATH"] = os.pathsep.join([os.path.dirname(tex), env.get("PATH", "")])
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _last_line(txt: str) -> str:
    lines = [l.strip() for l in (txt or "").splitlines() if l.strip()]
    return lines[-1] if lines else ""


def summary(errs: str, n: int = 220) -> str:
    """Erreurs lisibles en une ligne : la cause d'une trace Python (sa dernière ligne), la première erreur LaTeX
    (« ! … »), sinon les premières erreurs."""
    if "Traceback (most recent call last)" in (errs or ""):
        return _last_line(errs)[:n]
    lines = [l.strip() for l in (errs or "").splitlines() if l.strip()]
    bang = [l for l in lines if l.startswith("! ")]
    if bang:
        files = [l.split(" : ")[0].lstrip("✗ ").strip() for l in lines if "erreur LaTeX" in l]
        return (f"erreur LaTeX ({', '.join(files)}) : {bang[0]}")[:n]
    return (" | ".join(lines[:3]))[:n] or "erreur inconnue"


BLOCKING = re.compile(r"terme interdit|telephone|disponibilite|en cours|\d+ pages|erreur LaTeX|pdflatex|police non embarquee|"
                      r"image raster|aucun CV_", re.I)


def _blocking(errs: str) -> bool:
    """Erreur qui touche la vérité ou la règle du skill (terme interdit, téléphone, page en trop, LaTeX) : à corriger par
    l'IA. Les autres (lecture ATS, mise en forme) laissent un CV juste : gardé avec avertissement, sans tout régénérer."""
    return bool(BLOCKING.search(errs or ""))


def _latex_only(errs: str) -> bool:
    """build.py n'a échoué qu'à la compilation (aucune règle du skill en cause) : souvent l'installation LaTeX."""
    lines = [l for l in (errs or "").splitlines() if l.strip()]
    return bool(lines) and all("erreur LaTeX" in l or l.strip().startswith(("! ", "l.")) for l in lines)


def _build_full(w: Path, out: Path) -> tuple[bool, str, str]:
    """(réussi, erreurs signalées par build.py, plantage). plantage : cause d'un arrêt de build.py sur une exception."""
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", str(RUNNER), str(SKILL / "scripts" / "build.py"), str(w),
                            "--kw", "kw.txt", "--kw-opt", "kw-opt.txt", "--out", str(out)],
                           cwd=w, env=_build_env(), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=300, creationflags=0x08000000 if IS_WIN else 0)
    except subprocess.TimeoutExpired:
        return False, "", "délai dépassé (MiKTeX télécharge peut-être des paquets)"
    log_txt = r.stdout + r.stderr
    # « ✗ fichier : erreur LaTeX » suivi des lignes « ! … » et « l.42 … » de pdflatex : la vraie cause
    errs = [l.strip() for l in log_txt.splitlines() if "ERREUR" in l or l.startswith("✗") or l.strip().startswith(("! ", "l."))]
    if r.returncode and "Traceback (most recent call last)" in log_txt:
        return False, "\n".join(errs), _last_line(log_txt)[:300]
    return r.returncode == 0, "\n".join(errs)[:3000] or log_txt[-1500:], ""


def _skill_module():
    spec = importlib.util.spec_from_file_location("skill_build", SKILL / "scripts" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _docs(d: Path):
    return [p for p in d.glob("*.pdf") if p.name.startswith(("CV_", "Lettre_", "Cover_Letter_"))]


def _build_lite(w: Path, out: Path, kws: list) -> tuple[bool, str]:
    """Sans poppler/qpdf : compilation (2 passes), 1 page, règles du skill, mots-clés lus par pypdf, neutralisation."""
    errs = []
    mod = _skill_module()
    for f in sorted(w.glob("*.tex")):
        for _ in range(2):
            ok, err = generator.compile_tex(f)
            if not ok:
                break
        if not ok:
            errs.append(f"{f.name} : {err}")
            continue
        pdf = f.with_suffix(".pdf")
        if generator.pages(pdf) != 1:
            errs.append(f"{f.name} : {generator.pages(pdf)} pages (1 attendue)")
        errs += [f"{f.name} : {e}" for e in mod.check_regles(str(f))]
        if f.name.startswith("CV_"):
            e2, _n = mod.check_entries(str(f))
            errs += e2
            miss = generator.ats_missing(pdf, kws)
            if miss:
                errs.append(f"mots-clés non lus dans le PDF : {miss}")
        generator.neutralise(pdf)
    if not errs:
        out.mkdir(parents=True, exist_ok=True)
        for p in _docs(w):
            shutil.copy(p, out)
    return not errs, "\n".join(errs)


def _write(w: Path, d: dict):
    if w.exists():
        shutil.rmtree(w, ignore_errors=True)
    w.mkdir(parents=True, exist_ok=True)
    for f in (SKILL / "assets").iterdir():
        if f.is_file():
            shutil.copy(f, w)
    for f in (SKILL / "assets" / "icons").glob("*.pdf"):
        shutil.copy(f, w)
    ent = re.sub(r"[^\w]+", "_", d["company"]).strip("_")[:30] or "Entreprise"
    en = d.get("lang") == "en"
    (w / f"CV_Abderraouf_Derardja_{ent}.tex").write_text(d["cv_tex"], encoding="utf-8")
    (w / (f"Cover_Letter_Abderraouf_Derardja_{ent}.tex" if en else f"Lettre_Abderraouf_Derardja_{ent}.tex")).write_text(
        d["lettre_tex"], encoding="utf-8")
    (w / "kw.txt").write_text("\n".join(d.get("kw", [])) + "\n", encoding="utf-8")
    (w / "kw-opt.txt").write_text("\n".join(d.get("kw_opt", [])) + "\n", encoding="utf-8")


def generate(offer: dict) -> dict:
    """Renvoie {ok, eligible, reason, cv, lm, lettre_txt, message_court, poste, dossier, lang, warnings}.
    retry=True : l'IA n'a pas pu répondre (clé, crédit, réseau) ; l'offre est gardée pour plus tard."""
    if not providers.ready():
        return {"ok": False, "eligible": None, "retry": True, "reason": "aucune clé d'IA configurée"}
    if load_settings().get("cv_mode") == "rapide":  # modèles + adaptation courte : ~4 fois plus rapide que le skill complet
        return _fallback(offer, "mode CV rapide")
    if local_first():  # petit modèle local : le skill complet le dépasse ; modèles du profil + adaptation courte
        return _fallback(offer, "modèle local : modèles du profil adaptés")
    user = (f"OFFRE (source : {offer.get('site')}, URL : {offer.get('url')})\nEntreprise : {offer.get('company')}\n"
            f"Intitulé : {offer.get('title')}\nLieu : {offer.get('location')}\n\n{(offer.get('description') or '')[:14000]}" + (("\n\n" + rules.prompt_block(offer)) if rules.prompt_block(offer) else ""))
    conv = _conversation()
    try:
        d = conv.ask(user)
    except Exception as e:
        log(f"skill CV : {e.__class__.__name__} {str(e)[:120]}")
        return _fallback(offer, f"skill indisponible ({e.__class__.__name__})")
    if d is None:
        return {"ok": False, "eligible": False, "reason": "réponse illisible ou déclinée"}
    if not d["eligible"]:
        return {"ok": False, "eligible": False, "reason": d.get("reason", "non éligible"), "poste": d.get("poste")}
    dossier = re.sub(r"[^\w]+", "_", d.get("dossier") or d["company"]).strip("_")[:60]
    w = WORK / dossier
    out = w / "out"
    full = _tools_ok() and not _BROKEN
    warnings = []
    for attempt in range(2):
        _write(w, d)
        lite_done = False
        if full:
            ok, errs, crash = _build_full(w, out)
            if crash:
                # build.py lui-même en panne (outil absent, exception) : l'IA ne peut rien y corriger
                log(f"build.py du skill arrêté : {crash} — contrôle allégé à la place")
                if not crash.startswith("délai"):
                    _BROKEN["full"] = crash
                full = False
            elif not ok and _latex_only(errs):
                # pdflatex lancé par build.py échoue (polices du skill introuvables par MiKTeX…) : même .tex compilé par
                # StageAuto (carte des polices chargée explicitement), avec les contrôles du skill
                ok2, errs2 = _build_lite(w, out, d.get("kw", []))
                log(f"build.py : {summary(errs)} — compilation par StageAuto : {'réussie' if ok2 else summary(errs2)}")
                lite_done = True
                if ok2:
                    _BROKEN["full"] = "erreur LaTeX dans build.py ; compilation de StageAuto utilisée"
                    full, ok, errs = False, True, ""
                else:
                    errs = errs2 or errs
        if not full and not lite_done:
            ok, errs = _build_lite(w, out, d.get("kw", []))
        if ok:
            break
        log(f"build {dossier} : {summary(errs)} (tentative {attempt + 1})")
        if attempt == 0 and not _blocking(errs):
            warnings.append("contrôles ATS non parfaits (pas de correction, économie de jetons) : " + summary(errs, 200))
            break
        if attempt == 0:
            try:
                d2 = conv.ask("build.py signale ces erreurs. Corrige-les en une fois et renvoie l'objet complet "
                              f"(mêmes champs) :\n{errs}")
            except Exception as e:
                log(f"correction : {e.__class__.__name__}")
                d2 = None
            if not d2 or not d2.get("cv_tex"):
                break
            d = d2
    pdfs = _docs(out) if out.exists() else []
    if len(pdfs) < 2:
        # dernier recours : PDF compilés mais contrôles non parfaits, s'ils tiennent sur 1 page
        cand = _docs(w)
        if len(cand) >= 2 and all(generator.pages(p) == 1 for p in cand):
            out.mkdir(parents=True, exist_ok=True)
            for p in cand:
                generator.neutralise(p)
                shutil.copy(p, out)
            pdfs = _docs(out)
            warnings.append("contrôles ATS non parfaits : " + summary(errs, 300))
        else:
            return _fallback(offer, "build du skill en échec : " + summary(errs))
    cv = next(p for p in pdfs if p.name.startswith("CV_"))
    lm = next(p for p in pdfs if not p.name.startswith("CV_"))
    lettre_tex = next(iter(w.glob("Lettre_*.tex")), None) or next(iter(w.glob("Cover_Letter_*.tex")), None)
    lettre_txt = generator.plain(lettre_tex.read_text(encoding="utf-8")) if lettre_tex else ""
    (out / "lettre.txt").write_text(lettre_txt, encoding="utf-8")
    return {"ok": True, "eligible": True, "cv": str(cv), "lm": str(lm), "lettre_txt": lettre_txt,
            "message_court": d.get("message_court", "")[:400], "poste": d.get("poste") or offer.get("title"),
            "dossier": dossier, "lang": d.get("lang", "fr"), "company": d.get("company") or offer.get("company"),
            "ville": d.get("ville"), "recruteur": d.get("recruteur"), "warnings": warnings, "folder": str(out)}


def local_first() -> bool:
    order = providers.providers_in_order(task="cv")
    return bool(order) and order[0] in providers.LOCAL


def _fallback(offer, why):
    log(f"repli modèles : {why}")
    try:
        t = llm.tailor_local(offer) if local_first() else llm.tailor(offer)
    except Exception as e:  # problème d'IA (clé, crédit, réseau) : l'offre sera reprise, pas ignorée
        return {"ok": False, "eligible": None, "retry": True, "reason": f"{why} ; IA indisponible ({e.__class__.__name__})"}
    if t is None:
        return {"ok": False, "eligible": None, "retry": True, "reason": why}
    if not t.get("eligible"):
        return {"ok": False, "eligible": False, "reason": t.get("reason", "non éligible")}
    o = t
    dossier = generator.slug(t["e"]) + "_" + generator.slug(t["s"])
    r = generator.generate(o, WORK / dossier / "out", english_rules=(SKILL / "references" / "english.md").read_text(encoding="utf-8"),
                           translate=llm.translate_tex)
    if not r.get("ok"):
        return {"ok": False, "eligible": True, "reason": r.get("error", "génération impossible")}
    r.update({"eligible": True, "message_court": t.get("message_court", ""), "poste": t.get("poste"), "dossier": dossier,
              "lang": t.get("lang"), "company": t.get("e"), "ville": t.get("ville"), "recruteur": t.get("recruteur"),
              "warnings": r.get("warnings", []) + [why]})
    return r
