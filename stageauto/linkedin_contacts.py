"""LinkedIn : recruteur et manager de chaque candidature (InMail si possible, sinon invitation avec note),
puis suivi des acceptations (message + CV + lettre + certificats). Exécuté par l'agent navigateur."""
import re
from pathlib import Path
from urllib.parse import quote

from . import db
from .agent import Agent
from .config import CANDIDATURES, PROFILE, load_settings
from .forms import nrm
from .notify import log

PEOPLE_JS = r"""() => { const out = []; const seen = new Set();
  document.querySelectorAll('a[href*="/in/"]').forEach(a => {
    const url = a.href.split('?')[0].replace(/\/$/, ''); if (seen.has(url) || !/\/in\/[^/]+$/.test(url)) return;
    const card = a.closest('li, [data-view-name], section') || a;
    const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(s => s);
    if (!lines.length) return; seen.add(url);
    out.push({url, name: lines[0].replace(/\s*(•.*|\(.*\))$/, ''), headline: lines.slice(1, 3).join(' | ')}); });
  return out.slice(0, 60); }"""


def _short(p):
    return re.sub(r"\s+", " ", p or "")[:60].strip()


def note_text(poste, company):
    t = (f"Bonjour <Prénom>, je viens de postuler au stage {_short(poste)} chez {company} (6 mois dès février 2027). "
         "M2 Sécurité et Réseaux à Paris-Saclay, ravi d'échanger. Abderraouf")
    if len(t) > 185:  # <Prénom> sera remplacé : garder de la marge sous 200 caractères
        t = t.replace(_short(poste), _short(poste)[:30])
    return t


def followup_text(poste, company):
    s = load_settings()
    defense = any(nrm(d) in nrm(company) for d in s.get("defense_companies", []))
    t = (f"Bonjour <Prénom>, merci pour la mise en relation. J'ai postulé à votre offre de stage {poste} "
         "(6 mois dès février 2027). Je vous joins mon CV, ma lettre de motivation et mes certificats : pourriez-vous y "
         "jeter un coup d'œil ? Je serais ravi d'en échanger 10 minutes.")
    if defense:
        t += f" Je précise que je suis de nationalité {PROFILE.get('nationalite', '').lower()}."
    return t + " Abderraouf Derardja"


def files_for(folder: str | None, cv=None, lm=None):
    s = load_settings()
    f = {"cv": cv, "lm": lm}
    if folder and Path(folder).exists():
        d = Path(folder)
        f["cv"] = f["cv"] or next((str(p) for p in d.glob("CV_*.pdf")), None)
        f["lm"] = f["lm"] or next((str(p) for p in list(d.glob("Lettre_*.pdf")) + list(d.glob("Cover_Letter_*.pdf"))), None)
    certs = s.get("certificats_pdf")
    if certs and Path(certs).exists():
        f["certs"] = certs
    return f


async def contact_for(browser, offer: dict, folder: str):
    s = load_settings()
    inv_left = max(0, s["max_invites_per_day"] - db.invites_today())
    inm_left = max(0, s["max_inmails_per_day"] - db.inmails_today())
    if inv_left == 0 and inm_left == 0:
        log("LinkedIn : plus d'invitations ni d'InMails aujourd'hui")
        return []
    company, poste = offer["company"], offer.get("poste") or offer["title"]
    known = [r["name"] for r in db.q("SELECT name FROM contacts WHERE company LIKE ? OR company=''", (f"%{company[:12]}%",))][:40]
    files = files_for(folder, offer.get("cv"), offer.get("lm"))
    words = "cybersécurité" if not re.search(r"devops|cloud|réseau|reseau|network", nrm(poste)) else poste.split()[0]
    goal = (
        f"Après ma candidature au stage « {poste} » chez {company}, trouve sur LinkedIn 1 à 2 personnes ACTUELLEMENT chez "
        f"{company} (vérifie l'entreprise actuelle sur le profil) : (1) chargé(e) de recrutement / talent acquisition des stages "
        f"ou de l'équipe ; (2) manager de l'équipe du poste. Recherches utiles : "
        f"https://www.linkedin.com/search/results/people/?keywords={quote(company + ' talent acquisition')} puis "
        f"https://www.linkedin.com/search/results/people/?keywords={quote(company + ' manager ' + words)}.\n"
        f"Ne contacte PAS ces personnes (déjà contactées) : {', '.join(known) or 'aucune'}.\n"
        f"Pour chaque personne retenue, sur son profil :\n"
        f"- déjà relation (1er) : bouton Message, envoie le texte suivant (remplace <Prénom>) puis, en messages séparés, "
        f"upload cv, lettre, certificats via « Joindre un fichier » : « {followup_text(poste, company)} » ;\n"
        f"- sinon, si « Message » permet d'écrire sans invitation (InMail Premium / profil ouvert) et qu'il reste des InMails "
        f"({inm_left} aujourd'hui, à réserver aux plus pertinents) : objet « Candidature stage {_short(poste)} – février 2027 », "
        f"texte : « {followup_text(poste, company).replace('merci pour la mise en relation. ', '')} » ;\n"
        f"- sinon invitation avec note (« Se connecter »/« Inviter », parfois sous « Plus », puis « Ajouter une note ») si des "
        f"invitations restent ({inv_left} aujourd'hui), note exacte (< 200 caractères, remplace <Prénom>) : "
        f"« {note_text(poste, company)} ».\n"
        "Termine par finish(status='done', contacts=[{name, role, profile_url, action}]) avec action invite/inmail/message/none.")
    tab = await browser.new_tab("https://www.linkedin.com/feed/")
    try:
        res = await Agent(browser, tab, "linkedin_contacts", files, offer).run(goal, max_steps=45)
    finally:
        try:
            await tab.close()
        except Exception:
            pass
    done = []
    for c in res.get("contacts") or []:
        st = {"invite": "INVITE", "inmail": "INMAIL", "message": "ENVOYE"}.get(c.get("action"), None)
        if not st:
            continue
        db.add_contact(company, poste, c["name"], c.get("role", ""), c.get("profile_url"), st, folder, "agent")
        done.append(f"{c['name']} ({c.get('role', '')[:40]}) : {st}")
    return done


async def follow_ups(browser):
    """Personnes de contacts devenues relations : message + pièces jointes ; réponses signalées."""
    pending = db.q("SELECT * FROM contacts WHERE status='INVITE' AND profile_url IS NOT NULL")
    if not pending:
        return []
    tab = await browser.new_tab("https://www.linkedin.com/mynetwork/invite-connect/connections/")
    out = []
    try:
        recent = {p["url"].rstrip("/").lower() for p in (await tab.eval(PEOPLE_JS) or [])}
        for c in pending:
            if c["profile_url"].rstrip("/").lower() not in recent:
                continue
            files = files_for(c["folder"])
            goal = (f"{c['name']} a accepté mon invitation. Ouvre {c['profile_url']}, clique « Message » et lis TOUTE la "
                    f"conversation. Si {c['name'].split()[0]} m'a écrit : n'envoie rien, finish(status='replied', reason=<résumé>). "
                    "Si un message « merci pour la mise en relation » a déjà été envoyé : finish(status='done', reason='déjà envoyé'). "
                    f"Sinon envoie : « {followup_text(c['offer'] or 'de stage', c['company'] or '').replace('<Prénom>', c['name'].split()[0])} » "
                    "puis, un fichier par message, upload cv, lettre, certificats (« Joindre un fichier », puis Envoyer). "
                    "finish(status='done').")
            res = await Agent(browser, tab, "linkedin_contacts", files, {"company": c["company"], "title": c["offer"]}).run(goal, max_steps=25)
            st = "REPONDU" if res["status"] == "replied" else ("ENVOYE" if res["status"] == "done" else "INVITE")
            db.x("UPDATE contacts SET status=?, updated=? WHERE id=?", (st, db.now(), c["id"]))
            out.append(f"{c['name']} ({c['company']}) : {st}" + (f" — {res.get('reason', '')[:100]}" if st == "REPONDU" else ""))
    finally:
        await tab.close()
    return out


def envoyees_dir(dossier: str) -> Path:
    d = CANDIDATURES / "envoyees" / dossier
    d.mkdir(parents=True, exist_ok=True)
    return d
