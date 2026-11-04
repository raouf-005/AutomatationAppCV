"""Appels d'IA de haut niveau (fournisseur au choix, voir providers.py) :
- tailor : qualification + champs d'adaptation du CV par modèles (repli du skill) ;
- answer_question : réponse à une question de formulaire inconnue (mémorisée ensuite) ;
- translate_tex : traduction d'un .tex (offre en anglais, repli)."""
import re

from . import providers, rules
from .config import BANNED_TOOLS, PROFILE, SKILL_DIR, TPL_DIR, load_settings
from .notify import log

client = providers.anthropic_client  # compatibilité


def reset():
    providers.reset()


S = {"type": "string"}
TAILOR_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["eligible", "reason", "lang", "e", "s", "dom", "poste", "domaine", "equipe", "ville", "accroche", "pourquoi",
                 "kw", "puces", "skills", "savoir", "preuve", "message_court", "recruteur"],
    "properties": {
        "eligible": {"type": "boolean"}, "reason": S, "lang": {"type": "string", "enum": ["fr", "en"]},
        "e": S, "s": S, "dom": {"type": "string", "enum": ["soc", "grc", "devops", "reseau", "offensif"]},
        "poste": S, "domaine": S, "equipe": S, "ville": S, "accroche": S, "pourquoi": S,
        "kw": {"type": "array", "items": S},
        "puces": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["old", "new"],
                                             "properties": {"old": S, "new": S}}},
        "skills": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["label", "value"],
                                              "properties": {"label": S, "value": S}}},
        "savoir": S, "preuve": S, "message_court": S, "recruteur": S},
}
ANSWER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer", "sensitive"],
                 "properties": {"answer": S, "sensitive": {"type": "boolean"}}}


def _tailor_system():
    parts = [f"=== MODELE {p.stem} ===\n{p.read_text(encoding='utf-8')}" for p in sorted(TPL_DIR.glob("cv_*.tex"))]
    profil = (SKILL_DIR / "references" / "profil.md").read_text(encoding="utf-8")
    return (
        f"Tu adaptes le CV LaTeX et la lettre de {PROFILE.get('nom_complet', 'du candidat')} à une offre de stage. "
        "Tu n'inventes RIEN : seules les compétences et expériences du profil ci-dessous existent. "
        "Chiffres autorisés uniquement : ceux du profil. "
        f"Jamais : {', '.join(BANNED_TOOLS)} ni compétence absente du profil ; « LLM » toujours avec « en cours ». "
        "Éligible seulement : stage de fin d'études / 6 mois dès février 2027 en cybersécurité, SOC/CERT, GRC, pentest, "
        "admin systèmes et réseaux, DevOps/DevSecOps, cloud/infra, automatisation, IA appliquée à la cyber ; non éligible si "
        "alternance, CDI, moins de 6 mois, autre domaine, nationalité française/citoyenneté UE/habilitation exigée. "
        "puces : 2 à 5, old = début EXACT (20+ car.) d'une puce \\item du modèle du domaine, new = 95-105 car., au plus 2 \\kw{}. "
        "skills : label = libellé existant d'une \\skillline, value = contenu seul (88 car. max). preuve : vide ou un paragraphe. "
        "message_court < 400 caractères sans point-virgule.\n\n"
        f"=== PROFIL ===\n{profil}\n\n" + "\n\n".join(parts))


def tailor(offer: dict) -> dict | None:
    user = (f"Entreprise : {offer.get('company')}\nIntitulé : {offer.get('title')}\nLieu : {offer.get('location')}\n"
            f"Source : {offer.get('site')}\n\nDescription :\n{(offer.get('description') or '')[:12000]}"
            + (("\n\n" + rules.prompt_block(offer)) if rules.prompt_block(offer) else ""))
    conv = providers.JsonConversation(_tailor_system(), TAILOR_SCHEMA, max_tokens=16000, effort="medium", name="adaptation")
    d = conv.ask(user)
    if d is None:
        log("adaptation : réponse illisible ou déclinée")
    return d


def answer_question(question: str, options: list | None, offer: dict, kind: str) -> dict | None:
    import json
    s = load_settings()
    prof = json.dumps({**PROFILE, **s.get("custom_answers", {})}, ensure_ascii=False)
    opt = ("\nOptions : " + " | ".join(options[:40])) if options else ""
    msg = (f"Formulaire de candidature (stage chez {offer.get('company')} : {offer.get('title')}).\n"
           f"Type de champ : {kind}\nQuestion : {question}{opt}\n\nProfil autorisé : {prof}\n"
           "Réponds avec une information du profil uniquement ; pour un choix, le libellé EXACT d'une option ; vide si "
           "l'information manque. sensitive = vrai si la question demande une donnée sensible absente du profil (titre de "
           "séjour, casier, identité officielle, banque, date de naissance). Années d'expérience : estimation prudente. "
           "Source de l'offre : le site où elle a été trouvée, sinon « Autre ». Question oui/non sur une compétence : oui "
           "seulement si le profil la contient.")
    try:
        return providers.JsonConversation("Assistant de candidature. Réponses courtes et exactes.", ANSWER_SCHEMA,
                                          max_tokens=2000, effort="low", name="reponse").ask(msg)
    except Exception as e:
        log(f"IA question : {e.__class__.__name__}")
        return None


def translate_tex(tex: str, english_rules: str) -> str | None:
    out = providers.complete_text("Traduis ce document LaTeX en anglais selon ces règles. Garde toutes les macros, "
                                  "remplace \\usepackage[french]{cvstyle} par \\usepackage[english]{cvstyle}. "
                                  f"Réponds uniquement par le .tex complet.\n\nRÈGLES :\n{english_rules}\n\nDOCUMENT :\n{tex}")
    if not out:
        return None
    out = re.sub(r"^```(?:latex|tex)?\s*|```\s*$", "", out.strip())
    return out if "\\begin{document}" in out else None
