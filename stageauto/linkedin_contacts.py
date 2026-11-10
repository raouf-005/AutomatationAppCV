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


SEARCH_JS = r"""() => { const out = []; const seen = new Set();
  const root = document.querySelector('main') || document;
  root.querySelectorAll('a[href*="/in/"]').forEach(a => {
    const url = a.href.split('?')[0].replace(/\/$/, ''); if (seen.has(url) || !/\/in\/[^/]+$/.test(url)) return;
    const card = a.closest('li, [data-view-name=search-entity-result-universal-template], [data-chameleon-result-urn]') || a.parentElement;
    const lines = (card ? card.innerText : '').split('\n').map(s => s.trim()).filter(s => s);
    if (lines.length < 2) return; seen.add(url);
    const deg = (lines.join(' ').match(/\b(1er|2e|3e|1st|2nd|3rd)\b/) || [''])[0];
    out.push({url, name: lines[0].replace(/\s*(•.*|\(.*\))$/, ''), text: lines.slice(1, 8).join(' | ').slice(0, 400), degree: deg}); });
  return out.slice(0, 30); }"""

RH_RE = re.compile(r"recrut|talent|acquisition|\brh\b|ressources humaines|human resources|\bhr\b|\bdrh\b|campus|sourcing|"
                   r"people (partner|ops|operations)|staffing|charge(e)? de mission rh", re.I)
JUNIOR_RH_RE = re.compile(r"stag|campus|jeunes dipl|early career|graduate|universit|ecoles|relations ecoles|alternan", re.I)
MGR_RE = re.compile(r"manager|responsable|\bhead\b|\blead\b|directeur|directrice|\bchef\b|\bcto\b|\bciso\b|\brssi\b|"
                    r"team leader|engineering manager|\bvp\b|practice", re.I)
OUT_RE = re.compile(r"etudiant|stagiaire|alternant|apprenti|student|\bintern\b|a la recherche|en recherche|open to work|"
                    r"recherche (un|une|d'un) (stage|alternance|emploi)|freelance|retraite", re.I)
EX_RE = re.compile(r"\b(ex|ancien|ancienne|former)[- ]", re.I)
DOMAINS = [(r"soc|siem|detection|blue ?team|incident", "SOC OR cybersécurité OR sécurité"),
           (r"pentest|red ?team|offensi|audit", "pentest OR cybersécurité OR sécurité"),
           (r"reseau|network|telecom|noc", "réseau OR network OR infrastructure"),
           (r"devops|devsecops|cloud|sre|kubernetes", "DevOps OR cloud OR infrastructure"),
           (r"cyber|securit|grc|iam|risk", "cybersécurité OR sécurité OR security")]


def company_name(company: str) -> str:
    """Nom de recherche : sans forme juridique ni mention entre parenthèses (« Thales SIX GTS France SAS » -> « Thales SIX GTS »)."""
    c = re.sub(r"\(.*?\)", " ", company or "")
    c = re.sub(r"\b(sas|sa|sasu|sarl|se|groupe|group|france|inc|ltd|gmbh|plc)\b\.?", " ", c, flags=re.I)
    return re.sub(r"\s+", " ", c).strip(" -,") or (company or "").strip()


def domain_words(poste: str) -> str:
    p = nrm(poste)
    return next((w for pat, w in DOMAINS if re.search(pat, p)), "cybersécurité OR sécurité OR réseau")


def people_queries(company: str, poste: str) -> list:
    c = company_name(company)
    q = lambda k: "https://www.linkedin.com/search/results/people/?keywords=" + quote(k) + "&origin=GLOBAL_SEARCH_HEADER"
    return [("rh", q(f'"{c}" (recruteur OR recruteuse OR recrutement OR "talent acquisition" OR "ressources humaines" OR RH OR campus)')),
            ("manager", q(f'"{c}" (manager OR responsable OR "head of" OR lead OR directeur OR RSSI) ({domain_words(poste)})'))]


def score_person(p: dict, company: str, poste: str, kind: str = "") -> dict:
    """Note une personne des résultats : travaille-t-elle chez l'entreprise, est-elle RH / recruteuse (surtout stages et
    campus) ou manager du domaine du poste ? Les étudiants, stagiaires, anciens salariés sont écartés."""
    t = nrm(f"{p.get('text') or p.get('headline') or ''}")
    key = nrm(company_name(company)).split()
    at_company = bool(key) and (key[0] in t)
    sc, role = (3 if at_company else -2), ""
    if RH_RE.search(t):
        sc, role = sc + 3 + (1 if JUNIOR_RH_RE.search(t) else 0), "recrutement / RH"
    elif MGR_RE.search(t):
        dom = any(w.strip().lower() in t for w in nrm(domain_words(poste)).split(" or "))
        sc, role = sc + (4 if dom else 0.5), "manager" + (" du domaine" if dom else "")
    if OUT_RE.search(t):
        sc -= 6
    if EX_RE.search(t):
        sc -= 3
    sc += {"1er": 1, "1st": 1, "2e": 0.5, "2nd": 0.5}.get(p.get("degree") or "", 0)
    return {**p, "score": sc, "role": role or kind, "at_company": at_company}


async def find_people(browser, company: str, poste: str, known: list, hirers: list | None = None) -> list:
    """Recherche sans IA des bonnes personnes : d'abord celle qui a publié l'offre (« équipe de recrutement »), puis
    deux recherches LinkedIn (RH / recrutement, managers du domaine), résultats notés et classés. L'agent n'a plus qu'à
    ouvrir ces profils : moins d'étapes, moins de jetons, meilleures cibles."""
    seen = {nrm(k) for k in known}
    best = {}
    for h in hirers or []:
        if nrm(h.get("name", "")) not in seen:
            best[h["url"]] = {**h, "text": h.get("headline", ""), "score": 9, "role": "a publié l'offre (équipe de recrutement)",
                              "at_company": True}
    tab = await browser.new_tab()
    try:
        for kind, url in people_queries(company, poste):
            try:
                await tab.goto(url)
                await tab.wait_for("() => document.querySelectorAll('main a[href*=\"/in/\"]').length > 1"
                                   " || /login|authwall|checkpoint/.test(location.href)", timeout=8)
                people = await tab.eval(SEARCH_JS) or []
            except Exception as e:
                log(f"recherche de contacts ({kind}) : {e.__class__.__name__}")
                continue
            for p in people:
                if nrm(p["name"]) in seen or "linkedin member" in nrm(p["name"]) or "utilisateur linkedin" in nrm(p["name"]):
                    continue
                sp = score_person(p, company, poste, kind)
                if sp["score"] >= 4 and sp["role"] and sp["score"] > best.get(p["url"], {}).get("score", -99):
                    best[p["url"]] = sp
    finally:
        try:
            await tab.close()
        except Exception:
            pass
    ranked = sorted(best.values(), key=lambda p: -p["score"])
    # au plus 4 : l'auteur de l'offre, le meilleur RH, le meilleur manager, puis le suivant
    out = []
    for pick in (lambda p: p["score"] >= 9, lambda p: "RH" in p["role"], lambda p: p["role"].startswith("manager"), lambda p: True):
        for p in ranked:
            if p not in out and pick(p):
                out.append(p)
                break
    return out[:4]


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
    shortlist = []
    if s.get("contacts_presearch", True):
        try:
            shortlist = await find_people(browser, company, poste, known, offer.get("hirers"))
        except Exception as e:
            log(f"recherche de contacts {company} : {e.__class__.__name__} {str(e)[:80]}")
    if shortlist:
        log(f"contacts {company} : {len(shortlist)} personne(s) repérée(s) sans IA — "
            + " ; ".join(f"{p['name']} ({p['role']})" for p in shortlist))
        who = ("Personnes déjà repérées et classées (ouvre directement leur profil, dans cet ordre ; vérifie sur le profil "
               "qu'elles sont ACTUELLEMENT chez l'entreprise ; retiens-en 2 au plus : 1 recrutement / RH et 1 manager si possible) :\n"
               + "\n".join(f"{i}. {p['name']} — {p['role']} — {(p.get('text') or '')[:110]} — {p['url']}"
                           for i, p in enumerate(shortlist, 1))
               + "\nSi aucune ne convient, cherche toi-même : ")
    else:
        who = (f"Trouve sur LinkedIn 1 à 2 personnes ACTUELLEMENT chez {company} (vérifie l'entreprise actuelle sur le profil) : "
               "(1) chargé(e) de recrutement / talent acquisition / RH (stages, campus) ; (2) manager de l'équipe du poste. "
               "Recherches utiles : ")
    searches = " puis ".join(u for _, u in people_queries(company, poste))
    goal = (
        f"Après ma candidature au stage « {poste} » chez {company}, contacte les bonnes personnes sur LinkedIn.\n{who}{searches}.\n"
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
    tab = await browser.new_tab(shortlist[0]["url"] if shortlist else "https://www.linkedin.com/feed/")
    try:
        res = await Agent(browser, tab, "linkedin_contacts", files, offer).run(goal, max_steps=30 if shortlist else 45)
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
