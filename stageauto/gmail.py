"""Gmail (dans Chrome) : e-mails des dernières 24 h demandant une action, confirmations d'adresse, réponses de recruteurs,
et alertes des sites d'emploi (leurs liens d'offres rattrapent ce que les recherches auraient manqué)."""
import asyncio
import re
from urllib.parse import parse_qs, urlparse

import httpx

from .config import load_settings
from .notify import log

ROWS_JS = r"""() => [...document.querySelectorAll('tr.zA')].slice(0, 60).map((r, i) => {
  const f = r.querySelector('.yX .yW [email], .yW span') || {};
  return {i, from: (f.getAttribute && f.getAttribute('email')) || '', name: (f.innerText || '').trim(), unread: r.classList.contains('zE'),
          subject: ((r.querySelector('.bog') || {}).innerText || '').trim(), snippet: ((r.querySelector('.y2') || {}).innerText || '').trim()}; })"""
# adresse du compte ouvert (titre « Boîte de réception - x@gmail.com - Gmail ») : deux numéros peuvent mener au même compte
ACCOUNT_JS = r"""() => { const m = (document.title || '').match(/[\w.+-]+@[\w-]+\.[\w.-]+/); return m ? m[0].toLowerCase() : ''; }"""
BODY_JS = r"""() => { const b = [...document.querySelectorAll('div.a3s')].pop();
  return b ? {subject: ((document.querySelector('h2.hP') || {}).innerText || '').trim(), text: (b.innerText || '').slice(0, 4000),
              links: [...b.querySelectorAll('a[href]')].map(a => a.href)} : null; }"""
UNREAD_JS = r"""() => { const b = [...document.querySelectorAll('[aria-label], [data-tooltip]')].find(e =>
  /^(marquer comme non lu|mark as unread)/i.test(e.getAttribute('aria-label') || e.getAttribute('data-tooltip') || ''));
  if (b) { b.click(); return true; } return false; }"""

ACTION = re.compile(r"entretien|interview|test technique|assessment|disponibilit|rendez-vous|visio|échange t[ée]l[ée]phonique|"
                    r"next steps?|prochaine [ée]tape|convocation|case study|coding (test|challenge)", re.I)
CONFIRM = re.compile(r"confirm(ez|er)? (votre|ton|your) (adresse|e-?mail|candidature)|v[ée]rifi(ez|er) (votre|your) (adresse|e-?mail)|"
                     r"finalisez votre candidature|verify your email", re.I)
REFUS = re.compile(r"malheureusement|unfortunately|ne pas donner suite|pas retenu|not (been )?selected|regret|"
                   r"autre candidat|other candidates?|pas (pu )?(donner|accorder) une suite favorable", re.I)
# offre acceptée : candidature retenue, proposition de stage, convention, « nous avons le plaisir… »
OFFER = re.compile(r"(votre )?(candidature|profil) (a été |est )?(retenue?|accept[ée]e?|s[ée]lectionn[ée]e?) (pour|au|à)|"
                   r"nous (avons le plaisir|sommes (heureux|ravis|heureuses|ravies)) de vous (annoncer|informer|proposer|accueillir|confirmer)|"
                   r"proposition (de stage|d.embauche|de contrat)|promesse d.embauche|convention de stage|offre de stage (vous est|pour vous|ci-jointe)|"
                   r"(we are|we're) (pleased|happy|delighted|excited|thrilled) to (offer|inform you that|welcome)|offer letter|"
                   r"(internship|job) offer|(welcome|bienvenue) (to the team|aboard|dans l.[ée]quipe|parmi nous)|"
                   r"f[ée]licitations[^.]{0,60}(retenu|stage|candidature|accept)", re.I)
# proposition d'échange : créneau, disponibilités, lien de prise de rendez-vous
SLOT = re.compile(r"cr[ée]neaux?|disponibilit[ée]s?|calendly|prendre (un )?rendez-vous|planifier|fixer (un|une) (entretien|[ée]change|appel|"
                  r"rendez-vous)|[ée]changer (avec vous|ensemble|de vive voix)|un (premier |bref |court )?([ée]change|appel|call|entretien)|"
                  r"appel t[ée]l[ée]phonique|schedule (a|an|your)|book (a|your) (time|slot|call|meeting)|pick a time|choose a time|"
                  r"seriez-vous disponible|êtes-vous disponible|are you available|would you be available|invitation", re.I)
BOOKING = re.compile(r"calendly\.com|//cal\.com/|calendar\.app\.google|calendar\.google\.com/calendar/(appointments|u/\d+/appointments)|"
                     r"outlook\.office(365)?\.com/(owa/)?(calendar|bookwithme)|bookings\.|meetings\.hubspot\.com|doodle\.com|zcal\.co|"
                     r"tidycal\.com|savvycal\.com|youcanbook\.me|teams\.microsoft\.com/l/meetup|meet\.google\.com/[a-z]|zoom\.us/j/", re.I)
ACK = re.compile(r"candidature (a bien été |est )?(reçue|envoyée|transmise)|bien reçu votre candidature|application (received|submitted)|"
                 r"thank you for applying|merci pour votre candidature", re.I)
ALERT = re.compile(r"alerte|alert|newsletter|offres? (du jour|similaires)|jobs? for you|recommand|nouvelles offres", re.I)
# expéditeurs d'alertes d'offres (sites d'emploi)
ALERT_FROM = re.compile(r"linkedin|indeed|hellowork|welcometothejungle|wttj|jobteaser|apec|glassdoor|jobijoba|meteojob|monster|"
                        r"studentjob|free-work|1jeune1solution|francetravail|cadremploi|smartrecruiters", re.I)
JOB_WORDS = re.compile(r"offres?|postes?|jobs?|emplois?|stages?|opportunit|internships?|candidat", re.I)
NEWSLETTER = re.compile(r"conseils?|astuces?|webinar|webinaire|guide|newsletter|d[ée]couvrez|[ée]v[ée]nement|salon", re.I)
SITE_OF = {"li": "linkedin", "in": "indeed", "hw": "hellowork", "wttj": "wttj", "jt": "jobteaser", "sr": "smartrecruiters",
           "or": "orange", "ap": "carrieres", "gh": "carrieres", "lv": "carrieres", "ab": "carrieres", "wk": "carrieres",
           "rc": "carrieres", "ps": "carrieres", "bz": "carrieres", "th": "carrieres", "wd": "carrieres"}
MAIL_LINKS_JS = r"""() => [...document.querySelectorAll('div.a3s a[href]')].map(a => ({href: a.href,
  text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 300)}))"""


def mail_urls() -> list:
    """Boîtes Gmail à lire (réglage « Comptes Gmail » : numéros u/0, u/1… ou adresses ; par défaut les deux premiers
    comptes connectés dans Chrome)."""
    s = load_settings()
    accs = [str(a).strip() for a in (s.get("gmail_accounts") or []) if str(a).strip()] or [str(s.get("gmail_index", 0))]
    return [f"https://mail.google.com/mail/u/{a}/#search/newer_than%3A1d" for a in dict.fromkeys(accs)]


def classify(txt: str) -> str:
    """offre (acceptation) > refus > entretien (créneau, échange) ; l'ordre compte : un refus poli contient parfois
    « félicitations pour votre parcours »."""
    if REFUS.search(txt):
        return "refus"
    if OFFER.search(txt):
        return "offre"
    if SLOT.search(txt) or ACTION.search(txt):
        return "entretien"
    return ""


async def check(browser, base: str = "", bases: list | None = None):
    """Toutes les boîtes : réponses des recruteurs (offre, entretien / créneau, refus), confirmations d'adresse, accusés
    de réception. Chaque réponse de recruteur est ouverte pour lire tout le message et repérer un lien de prise de
    rendez-vous ; un e-mail non lu le redevient."""
    res = {"actions": [], "confirmed": [], "refus": [], "replies": [], "acks": [], "login": False, "events": [], "accounts": []}
    for url in ([base] if base else bases or mail_urls()):
        await _check_box(browser, url, res)
    return res


async def _check_box(browser, base: str, res: dict):
    tab = await browser.new_tab(base)
    try:
        await tab.wait_for("() => document.querySelectorAll('tr.zA').length || /accounts\\.google/.test(location.host)"
                           " || /Aucun message|No messages/.test(document.body.innerText)", timeout=12)
        if "accounts.google.com" in (await tab.url()):
            res["login"] = True
            return
        acct = await tab.eval(ACCOUNT_JS) or base
        if acct in res["accounts"]:
            return  # même compte qu'une boîte déjà lue (numéro différent)
        res["accounts"].append(acct)
        await tab.tour("tr.zA", "E-mail", 4)
        opened = 0
        for r in await tab.eval(ROWS_JS) or []:
            txt = f"{r['subject']} {r['snippet']}"
            who = r["name"] or r["from"]
            if CONFIRM.search(txt):
                ok = await _confirm(browser, tab, r["i"])
                if ok:
                    res["confirmed"].append(f"{who} : {r['subject'][:80]}")
                await tab.goto(base)
                await tab.wait_for("() => document.querySelectorAll('tr.zA').length", timeout=10)
                continue
            if ALERT_FROM.search(f"{r['from']} {who}") and (ALERT.search(txt) or NEWSLETTER.search(r["subject"])):
                continue  # alerte d'offres ou lettre d'information d'un site d'emploi : lue par alerts(), pas une réponse
            kind = classify(txt)
            recruiting = kind or (not ALERT.search(txt) and re.search(r"stage|internship|candidature|application|recrut|entretien|"
                                                                       r"interview|poste|offre", txt, re.I))
            if ACK.search(txt) and kind != "offre":
                res["acks"].append(f"{who} {r['subject']} {r['snippet'][:120]}")
                if not kind:
                    continue
            if not recruiting:
                continue
            ev = {"kind": kind or "réponse", "who": who, "from": r["from"], "subject": r["subject"], "snippet": r["snippet"],
                  "account": acct, "link": ""}
            if opened < 8:  # le message entier : la phrase décisive et le lien de rendez-vous sont rarement dans l'aperçu
                opened += 1
                body = await _read(tab, r, base)
                if body:
                    ev["kind"] = classify(f"{txt} {body['text']}") or ev["kind"]
                    ev["excerpt"] = re.sub(r"\s+", " ", body["text"])[:300]
                    ev["link"] = next((h for h in body["links"] if BOOKING.search(h)), "")
                    if ev["link"] and ev["kind"] in ("réponse", ""):
                        ev["kind"] = "entretien"
            res["events"].append(ev)
            label = f"{who} : {r['subject'][:90]}"
            {"entretien": res["actions"], "offre": res["actions"], "refus": res["refus"]}.get(ev["kind"], res["replies"]).append(
                ("OFFRE : " if ev["kind"] == "offre" else "") + label)
    except Exception as e:
        log(f"Gmail : {e.__class__.__name__} {str(e)[:100]}")
    finally:
        await tab.close()


async def _read(tab, r, base):
    """Ouvre l'e-mail, lit son texte et ses liens, le remet en « non lu » s'il l'était, puis revient à la liste."""
    try:
        await tab.eval("(i) => { const r = document.querySelectorAll('tr.zA')[i]; if (r) r.click(); }", r["i"])
        await tab.wait_for("() => document.querySelectorAll('div.a3s').length", timeout=8)
        body = await tab.eval(BODY_JS)
        if body and body.get("subject") and r["subject"] and body["subject"][:40] != r["subject"][:40]:
            body = None  # un nouvel e-mail a décalé la liste : pas le bon message
        if r.get("unread"):
            await tab.eval(UNREAD_JS)
            await asyncio.sleep(0.5)
        await tab.goto(base)
        await tab.wait_for("() => document.querySelectorAll('tr.zA').length", timeout=10)
        return body
    except Exception:
        return None


async def _confirm(browser, tab, i) -> bool:
    """Ouvre l'e-mail et suit le lien « Confirmer / Vérifier » (dans un onglet séparé)."""
    try:
        await tab.eval("(i) => { const r = document.querySelectorAll('tr.zA')[i]; if (r) r.click(); }", i)
        await asyncio.sleep(3)
        href = await tab.eval(r"""() => { const a = [...document.querySelectorAll('div.a3s a[href]')].find(a =>
            /confirm|v[ée]rifi|finalis|activer|verify/i.test(a.innerText + ' ' + a.href)); return a ? a.href : null; }""")
        if not href:
            return False
        t2 = await browser.new_tab(href)
        await asyncio.sleep(5)
        await t2.close()
        return True
    except Exception:
        return False


def _unwrap(href: str) -> str:
    """Lien réécrit par Google (google.com/url?q=…) -> adresse d'origine."""
    u = urlparse(href or "")
    if u.netloc.endswith("google.com") and u.path == "/url":
        return (parse_qs(u.query).get("q") or [href])[0]
    return href


async def _resolve(client, url: str) -> str:
    """Lien de suivi d'un e-mail (click.…, email.…) -> adresse de l'offre, en suivant les redirections sans charger
    la page finale (5 sauts au plus)."""
    from urllib.parse import urljoin
    from .sites import key_from_url
    for _ in range(5):
        try:
            r = await client.get(url, follow_redirects=False)
        except Exception:
            return url
        loc = r.headers.get("location")
        if r.status_code not in (301, 302, 303, 307, 308) or not loc:
            return url
        url = urljoin(url, loc)
        if key_from_url(url):
            return url
    return url


def _canonical(key: str, url: str) -> str:
    pre, ident = key.split(":", 1)
    return {"li": f"https://www.linkedin.com/jobs/view/{ident}/", "in": f"https://fr.indeed.com/viewjob?jk={ident}",
            "hw": f"https://www.hellowork.com/fr-fr/emplois/{ident}.html", "or": f"https://orange.jobs/fr/fr/job/{ident}"
            }.get(pre, url.split("?")[0].split("#")[0])


async def alerts(browser, max_mails: int = 12, base: str = "") -> list:
    if base:
        return await _alerts_box(browser, max_mails, base)
    out, seen = [], set()
    for url in mail_urls():
        for o in await _alerts_box(browser, max_mails, url):
            if o["key"] not in seen:
                seen.add(o["key"])
                out.append(o)
    return out


async def _alerts_box(browser, max_mails: int, base: str) -> list:
    """Alertes d'offres reçues ces dernières 24 h (LinkedIn, Indeed, HelloWork, WTTJ, JobTeaser, APEC…) : leurs liens
    deviennent des offres, dédoublonnées ensuite avec celles des recherches."""
    from .sites import junk_line, key_from_url
    found, rows = {}, []
    tab = await browser.new_tab(base)
    try:
        await tab.wait_for("() => document.querySelectorAll('tr.zA').length || /accounts\\.google/.test(location.host)"
                           " || /Aucun message|No messages/.test(document.body.innerText)", timeout=12)
        if "accounts.google.com" in (await tab.url()):
            return []
        rows = [r for r in await tab.eval(ROWS_JS) or []
                if ALERT_FROM.search(f"{r['from']} {r['name']}") and (ALERT.search(f"{r['subject']} {r['snippet']}")
                                                                       or JOB_WORDS.search(r["subject"]))
                and not ACK.search(f"{r['subject']} {r['snippet']}") and not NEWSLETTER.search(r["subject"])]
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "Mozilla/5.0"}) as client:
            resolved = 0
            for r in rows[:max_mails]:
                await tab.eval("(i) => { const r = document.querySelectorAll('tr.zA')[i]; if (r) r.click(); }", r["i"])
                await tab.wait_for("() => document.querySelectorAll('div.a3s a[href]').length > 0", timeout=8)
                for l in await tab.eval(MAIL_LINKS_JS) or []:
                    lines = [x.strip() for x in (l["text"] or "").split("\n") if x.strip()]
                    title = lines[0] if lines else ""
                    url = _unwrap(l["href"])
                    key = key_from_url(url)
                    if not key and resolved < 25 and len(title) >= 8 and not junk_line(title) and \
                            re.search(r"click|clic|email|mail|track|link|redirect|lnk|r\.", urlparse(url).netloc + urlparse(url).path, re.I):
                        url = await _resolve(client, url)  # lien de suivi : on suit les redirections
                        resolved += 1
                        key = key_from_url(url)
                    site = SITE_OF.get(key.split(":")[0]) if key else None
                    if not site:
                        continue
                    o = found.get(key) or {"key": key, "site": site, "title": "", "company": "", "url": _canonical(key, url),
                                           "recent": True, "source": "alerte e-mail", "from_alert": r["name"] or r["from"]}
                    if len(title) >= 6 and not junk_line(title) and len(title) > len(o["title"]) and \
                            not re.search(r"voir|view|postuler|apply|toutes les|all jobs|se d[ée]sabonner|unsubscribe", title, re.I):
                        o["title"] = title
                        o["company"] = lines[1] if len(lines) > 1 and len(lines[1]) < 80 else o["company"]
                    found[key] = o
                await tab.goto(base)
                await tab.wait_for("() => document.querySelectorAll('tr.zA').length", timeout=10)
    except Exception as e:
        log(f"alertes Gmail : {e.__class__.__name__} {str(e)[:100]}")
    finally:
        await tab.close()
    offers = [o for o in found.values() if o["title"]]
    if offers:
        log(f"alertes e-mail : {len(offers)} offres lues dans {min(len(rows), max_mails)} alerte(s)")
    return offers
