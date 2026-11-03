"""Appels Claude : qualification + adaptation du CV et de la lettre (un seul appel par offre), réponses aux questions."""
import json
import re
from typing import List, Optional

import anthropic
from pydantic import BaseModel, Field

from . import vault
from .config import BANNED_TOOLS, PROFILE, SKILL_DIR, TPL_DIR, load_settings
from .notify import log

_client = None


def client():
    global _client
    if _client is None:
        key = vault.get_api_key()
        _client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    return _client


def reset():
    global _client
    _client = None


class Puce(BaseModel):
    old: str = Field(description="début EXACT (20+ caractères) d'une puce \\item existante du modèle")
    new: str = Field(description="nouvelle puce complète, 95-105 caractères, au plus 2 \\kw{...}, & écrit \\&")


class Skill(BaseModel):
    label: str = Field(description="libellé EXISTANT d'une \\skillline du modèle (ex. Cybersécurité)")
    value: str = Field(description="contenu seul, 88 caractères max, sans le libellé")


class Tailor(BaseModel):
    eligible: bool = Field(description="vrai seulement si stage de 6 mois (ou fin d'études) démarrant en 2027 "
                                        "dans le domaine, exigences remplies, pas de nationalité française / "
                                        "habilitation / outil interdit exigé")
    reason: str = Field(description="raison courte si non éligible, sinon vide")
    lang: str = Field(description="fr ou en : langue du descriptif de l'offre")
    e: str = Field(description="nom court de l'entreprise")
    s: str = Field(description="sujet court en 2-4 mots (pour le nom de dossier)")
    dom: str = Field(description="soc | grc | devops | reseau | offensif")
    poste: str = Field(description="intitulé court, sans F/H ni référence")
    domaine: str = Field(description="expression du profil (ex. détection et réponse à incident)")
    equipe: str
    ville: str
    accroche: str = Field(description="2 phrases sur le problème concret du poste")
    pourquoi: str = Field(description="2 phrases : pourquoi cette entreprise, engagement cité dans l'offre")
    kw: List[str] = Field(description="8 à 15 mots-clés EXACTS de l'offre qu'il possède réellement")
    puces: List[Puce]
    skills: List[Skill]
    savoir: str = Field(description="qualités exactes de l'offre (savoir-être)")
    preuve: Optional[str] = Field(default="", description="paragraphe de preuve sur l'axe principal, ou vide")
    message_court: str = Field(description="message de candidature < 400 caractères, sans point-virgule")
    recruteur: Optional[str] = Field(default="", description="nom du recruteur/manager s'il est cité dans l'offre")


def _system():
    tpl = TPL_DIR
    parts = [f"=== MODELE {p.stem} ===\n{p.read_text(encoding='utf-8')}" for p in sorted(tpl.glob("cv_*.tex"))]
    profil = (SKILL_DIR / "references" / "profil.md").read_text(encoding="utf-8")
    return (
        "Tu adaptes le CV LaTeX et la lettre d'Abderraouf Derardja (M2 Sécurité et Réseaux, Université Paris-Saclay) "
        "à une offre de stage. Tu n'inventes RIEN : seules les compétences et expériences du profil ci-dessous existent. "
        f"Chiffres autorisés uniquement : 3 serveurs, 6 personnes, plus de 10 scénarios, 100 %, Top 10 sur 2 500, 4e sur 375, 3 ans. "
        f"Jamais : {', '.join(BANNED_TOOLS)} ni compétence absente du profil ; « LLM » toujours avec « en cours ». "
        "Domaine visé : stages de fin d'études / 6 mois à partir de février 2027 en cybersécurité, SOC/CERT, GRC, pentest, "
        "admin systèmes et réseaux, DevOps/DevSecOps, cloud et infra, automatisation, IA appliquée à la cyber. "
        "Non éligible : alternance, CDI, moins de 6 mois, autre domaine, nationalité française ou habilitation exigée, "
        "outil interdit exigé comme indispensable, citoyenneté UE exigée. "
        "Puces : choisis 2 à 5 puces du modèle du domaine choisi et réécris-les avec les mots exacts de l'offre. "
        "Les mots-clés kw doivent être des compétences qu'il a réellement.\n\n"
        f"=== PROFIL (source de vérité) ===\n{profil}\n\n" + "\n\n".join(parts))


_SYS = None


def tailor(offer: dict) -> Optional[Tailor]:
    global _SYS
    if _SYS is None:
        _SYS = _system()
    s = load_settings()
    user = (f"Entreprise : {offer.get('company')}\nIntitulé : {offer.get('title')}\nLieu : {offer.get('location')}\n"
            f"Source : {offer.get('site')}\n\nDescription :\n{(offer.get('description') or '')[:12000]}")
    kwargs = dict(model=s.get("model", "claude-opus-5-5"), max_tokens=16000,
                  system=[{"type": "text", "text": _SYS, "cache_control": {"type": "ephemeral"}}],
                  messages=[{"role": "user", "content": user}], output_format=Tailor)
    try:
        r = _parse_with_fallback(kwargs)
    except anthropic.AuthenticationError:
        log("Clé API Anthropic absente ou invalide : ajoute-la dans Réglages", "error")
        raise
    if r is None:
        return None
    return r


def _parse_with_fallback(kwargs):
    """Repli serveur activé (le modèle de secours prend le relais si la requête est déclinée)."""
    try:
        r = client().messages.parse(**kwargs, extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                                    extra_body={"fallbacks": "default"})
    except anthropic.BadRequestError as e:
        if "fallback" not in str(e).lower():
            raise
        r = client().messages.parse(**kwargs)
    if r.stop_reason == "refusal":
        log("Claude a décliné cette offre : ignorée")
        return None
    return r.parsed_output


class Answer(BaseModel):
    answer: str = Field(description="réponse exacte à saisir ; pour un choix, le libellé EXACT d'une option ; "
                                    "vide si l'information n'est pas dans le profil")
    sensitive: bool = Field(description="vrai si la question demande une donnée sensible non fournie "
                                        "(titre de séjour, casier, identité officielle, banque, date de naissance)")


def answer_question(question: str, options: list | None, offer: dict, kind: str) -> Answer | None:
    s = load_settings()
    prof = json.dumps({**PROFILE, **s.get("custom_answers", {})}, ensure_ascii=False)
    opt = ("\nOptions : " + " | ".join(options[:40])) if options else ""
    msg = (f"Formulaire de candidature (stage chez {offer.get('company')} : {offer.get('title')}).\n"
           f"Type de champ : {kind}\nQuestion : {question}{opt}\n\n"
           f"Profil autorisé : {prof}\n"
           "Réponds avec une information du profil uniquement. Années d'expérience : estimation honnête et prudente. "
           "Source de l'offre : le site où elle a été trouvée, sinon « Autre ». Question oui/non sur une compétence : "
           "oui seulement si le profil la contient. Champ texte libre de motivation : 2 phrases sobres.")
    try:
        r = client().messages.parse(model=s.get("model", "claude-opus-5-5"), max_tokens=2000,
                                    messages=[{"role": "user", "content": msg}], output_format=Answer)
        if r.stop_reason == "refusal":
            return None
        return r.parsed_output
    except Exception as e:
        log(f"LLM question : {e.__class__.__name__}")
        return None


def translate_tex(tex: str, english_rules: str) -> str | None:
    """Offre en anglais : traduit le .tex complet (macros conservées)."""
    s = load_settings()
    try:
        with client().messages.stream(model=s.get("model", "claude-opus-5-5"), max_tokens=32000,
                                      messages=[{"role": "user", "content":
                                                 "Traduis ce document LaTeX en anglais selon ces règles. Garde toutes les "
                                                 "macros, remplace \\usepackage[french]{cvstyle} par "
                                                 "\\usepackage[english]{cvstyle}. Réponds uniquement par le .tex complet.\n\n"
                                                 f"RÈGLES :\n{english_rules}\n\nDOCUMENT :\n{tex}"}]) as st:
            m = st.get_final_message()
        out = "".join(b.text for b in m.content if b.type == "text")
        out = re.sub(r"^```(?:latex|tex)?\s*|```\s*$", "", out.strip())
        return out if "\\begin{document}" in out else None
    except Exception as e:
        log(f"traduction : {e.__class__.__name__}")
        return None
