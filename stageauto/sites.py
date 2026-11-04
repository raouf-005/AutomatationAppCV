"""Sites d'offres : vérification de connexion, recherche des offres du jour, lecture du descriptif, point de départ de
la candidature (ensuite confiée à l'agent). Lecture par JavaScript (rapide, sans appel à Claude)."""
import asyncio
import datetime as dt
import html
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from .config import load_settings
from .forms import NeedLogin
from .notify import log

LINKS_JS = r"""(pat) => { const re = new RegExp(pat); const out = {};
  // libellés de boutons (favoris, « Postuler »…) : jamais pris pour l'intitulé
  const junk = t => !t || /favori/i.test(t) || /^(postuler|candidater|voir l.offre|enregistrer|sauvegarder|partager)$/i.test(t);
  document.querySelectorAll('a[href]').forEach(a => { const m = a.href.match(re); if (!m) return;
    const card = a.closest('li, article, [data-testid*=card], [class*=card], [class*=Card]') || a.parentElement || a;
    const lines = (card.innerText || a.innerText || '').split('\n').map(s => s.trim()).filter(s => s);
    let t = (a.innerText || '').trim().split('\n')[0] || '';
    if (junk(t)) { const h = card.querySelector('h1, h2, h3, h4'); t = h ? (h.innerText || '').trim().split('\n')[0] : ''; }
    if (junk(t)) t = '';
    const o = out[m[1]];
    if (!o) out[m[1]] = {id: m[1], href: a.href.split('?')[0].split('#')[0], title: t, lines};
    else { if (lines.length > o.lines.length) o.lines = lines; if (!o.title && t) o.title = t; } });
  return Object.values(out); }"""

NOT_LOGGED_BTN = r"""(words) => [...document.querySelectorAll('a, button')].some(e => e.offsetParent !== null &&
  words.some(w => (e.innerText || '').trim().toLowerCase() === w))"""

INTERN_RE = re.compile(r"\b(stages?|stagiaires?|interns?|internships?|pfe|fin d.?[ée]tudes|graduation (project|internship)|"
                       r"end[- ]of[- ]studies)\b", re.I)
RECENT_RE = re.compile(r"(il y a \d+\s*(min|minute|heure|h\b)|aujourd|à l'instant|hier|nouveau|new|\d+\s*(h|hours?|minutes?) ago|"
                       r"today|just posted|il y a 1 j|1 day ago|publiée? (il y a|aujourd))", re.I)
ALREADY_RE = re.compile(r"Suivi de candidature|Candidature envoyée|Déjà postulé|Vous avez postulé|Applied\b|Candidaté", re.I)


FAV_RE = re.compile(r"^(?:ajouter|retirer|enregistrer|sauvegarder|supprimer)\s+(?:l['’]\s*offre|l['’]\s*annonce|cette offre)\b"
                    r"(.*?)(?:\s+(?:aux|à vos|à mes|des|de vos|de mes|dans vos|dans mes)\s+favoris)?\.?$", re.I)
BUTTON_RE = re.compile(r"^(?:postuler|candidater|voir l['’]offre|voir (?:plus|le détail)|en savoir plus|partager|enregistrer|"
                       r"sauvegarder|save|saved|apply|easy apply|candidature (?:simplifiée|facile|rapide)|nouveau|nouvelle|new|"
                       r"sponsorisée?|promoted|urgent|suivi de candidature|candidature envoyée|déjà postulé|vous avez postulé|"
                       r"applied|freelance|cdi|cdd|stage|alternance|temps plein|full[- ]time)$", re.I)


def junk_line(line: str) -> bool:
    """Libellé de bouton lu dans une carte d'offre (« Ajouter l'offre … aux favoris », « Postuler », « Nouveau »…) :
    ni un intitulé, ni une entreprise."""
    t = (line or "").strip()
    return bool(not t or not re.search(r"[A-Za-zÀ-ÿ]{2}", t) or FAV_RE.match(t) or BUTTON_RE.match(t)
                or re.search(r"\bfavoris?\b", t, re.I))


def clean_title(t: str) -> str:
    """« Ajouter l'offre Stage X aux favoris » -> « Stage X »."""
    t = re.sub(r"\s+", " ", t or "").strip()
    m = FAV_RE.match(t)
    return m.group(1).strip(" -–—:") if m and m.group(1).strip(" -–—:") else t


def card_offer(c, site, prefix, source):
    L = [l for l in c["lines"] if len(l) < 160]
    txt = " ".join(L)  # avant filtrage : « Nouveau », « il y a 2 h » servent à la fraîcheur
    title = clean_title(c.get("title") or "")
    if junk_line(title):
        title = next((clean_title(l) for l in L if not junk_line(l)), "")
    rest = [l for l in L if l != title and not junk_line(l) and clean_title(l) != title]
    return {"key": f"{prefix}:{c['id']}", "site": site, "title": title, "company": rest[0] if rest else "",
            "location": rest[1] if len(rest) > 1 else "", "url": c["href"], "recent": bool(RECENT_RE.search(txt)),
            "already": bool(ALREADY_RE.search(txt)), "source": source, "card": txt[:500]}


def kw_list(quick):
    """Passage complet : tous les mots-clés. Veille (toutes les 30 min) : 3 mots-clés qui tournent d'une veille à
    l'autre, pour que chaque mot-clé soit vérifié plusieurs fois par jour sans alourdir la veille."""
    k = load_settings()["keywords"] or ["stage cybersécurité"]
    if not quick or len(k) <= 3:
        return list(k)
    start = (int(time.time() // 1800) * 3) % len(k)
    return [k[(start + i) % len(k)] for i in range(3)]


def max_pages(quick) -> int:
    return 1 if quick else max(1, int(load_settings().get("max_result_pages", 4)))


DESC_SEL = ("#job-details p, #job-details li, .jobs-description__content p, .jobs-description__content li, "
            "#jobDescriptionText p, #jobDescriptionText li, main p, main li")


async def search_pages(browser, urls, pattern, wait_js, make, tour_sel=None, next_page=None, pages=1):
    """Ouvre chaque page de résultats, attend les cartes (au lieu d'une pause fixe) et les lit en un appel. Avec
    next_page, lit les pages suivantes tant qu'elles apportent des offres nouvelles (recherches triées par date et
    limitées à la période : rien ne passe entre deux pages)."""
    out = {}
    tab = await browser.new_tab()
    try:
        for base in urls:
            for n in range(1, pages + 1):
                url = base if n == 1 else next_page(base, n)
                await tab.goto(url)
                await tab.wait_for(wait_js, timeout=8)
                if tour_sel and n == 1:
                    await tab.tour(tour_sel, "Offre", 4)
                fresh = 0
                for c in await tab.eval(LINKS_JS, pattern) or []:
                    o = make(c)
                    if o:
                        fresh += o["key"] not in out
                        out[o["key"]] = o
                if fresh < 5 or not next_page:  # page presque vide ou déjà vue : fin des résultats
                    break
    finally:
        await tab.close()
    return list(out.values())


LOGIN_STATE_JS = r"""() => {
  if (document.readyState !== 'complete') return null;
  const where = location.pathname + location.search;
  if (/(sign_?in|signin|login|connexion|\/auth\b|authwall|checkpoint)/i.test(where))
    return {s: 'no', why: 'redirigé vers la page de connexion'};
  const els = [...document.querySelectorAll('a, button, [role=button], [role=menuitem]')];
  const txt = e => ((e.textContent || '') + ' ' + (e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || ''))
    .replace(/\s+/g, ' ').trim().toLowerCase();
  // présents même dans un menu fermé (textContent, pas innerText)
  const OUT = /(^|\b)(se d[ée]connecter|d[ée]connexion|log ?out|sign ?out)(\b|$)|mon (profil|compte|espace)|my (profile|account)|mes candidatures|my applications|espace candidat/;
  const IN = /^(se connecter|connexion|s'identifier|me connecter|je me connecte|log ?in|sign ?in)$/;
  const out = els.find(e => OUT.test(txt(e)));
  if (out) return {s: 'yes', why: '« ' + txt(out).slice(0, 40) + ' » présent'};
  const vis = els.find(e => e.offsetParent !== null && IN.test(txt(e)));
  if (vis) return {s: 'no', why: 'bouton « ' + txt(vis).slice(0, 30) + ' » visible'};
  return null;
}"""


@dataclass
class SiteDef:
    id: str
    name: str
    home: str
    login_url: str = ""
    needs_login: bool = True
    hint_url: str = ""
    extra: dict = field(default_factory=dict)

    async def login_state(self, tab):
        """(True | False | None, raison). None = pas d'indice clair : le site est utilisé quand même."""
        r = await tab.wait_for(LOGIN_STATE_JS, timeout=8)
        if not r:
            return None, "pas d'indice de connexion (site utilisé quand même)"
        return r["s"] == "yes", r["why"]

    async def logged_in(self, tab) -> bool:
        return (await self.login_state(tab))[0] is not False

    async def search(self, browser, since_hours, quick=False) -> list:
        return []

    async def details(self, tab, offer) -> dict:
        await tab.goto(offer["url"])
        await tab.wait_for("() => (document.querySelector('main') || document.body).innerText.length > 400", timeout=6)
        await tab.tour(DESC_SEL, "Lecture", 4)
        offer["description"] = await tab.text("main") or await tab.text("body")
        offer["meta"] = offer["description"][:700]
        offer["already"] = offer.get("already") or bool(ALREADY_RE.search(offer["description"][:3000]))
        return offer

    async def start(self, tab, offer) -> str:
        """Ouvre le point de départ de la candidature ; renvoie des précisions pour l'agent."""
        await tab.goto(offer.get("apply_url") or offer["url"])
        return ""


# ----------------------------------------------------------------------------------- LinkedIn
class LinkedIn(SiteDef):
    async def login_state(self, tab):
        # 1. cookie de session (HttpOnly, lu par Chrome) : indépendant de l'apparence de la page
        if any(c.get("name") == "li_at" for c in await tab.cookies(["https://www.linkedin.com/"])):
            return True, "session LinkedIn active (cookie li_at)"
        # 2. sinon, ce que montre la page (la barre de LinkedIn s'affiche après le chargement : on attend)
        r = await tab.wait_for(r"""() => {
            if (/login|authwall|\/uas\/|checkpoint|signup/.test(location.href)) return {s: 'no', why: 'redirigé vers la connexion LinkedIn'};
            if (document.querySelector('#global-nav, header.global-nav, nav.global-nav, .global-nav__me, img.global-nav__me-photo, ' +
                '[data-test-global-nav], .feed-identity-module, a[href*="/mynetwork/"], a[href*="/messaging/"]'))
              return {s: 'yes', why: 'barre de navigation LinkedIn connectée'};
            if (document.readyState === 'complete' && document.querySelector('input[type=password]'))
              return {s: 'no', why: 'formulaire de connexion LinkedIn'};
            return null; }""", timeout=12)
        if not r:
            return None, "page LinkedIn sans indice clair (essayé quand même)"
        return r["s"] == "yes", r["why"]

    async def search(self, browser, since_hours, quick=False):
        s = load_settings()
        qs = s.get("linkedin_searches") or ["stage cybersécurité"]
        tpr = f"r{int(max(3600, since_hours * 3600))}"
        urls = [f"https://www.linkedin.com/jobs/search/?keywords={quote(qs[0])}&location=France&f_E=1&f_TPR={tpr}&sortBy=DD&f_AL=true",
                f"https://www.linkedin.com/jobs/search/?keywords={quote(qs[-1])}&location=France&f_E=1&f_TPR={tpr}&sortBy=DD"]
        if quick:
            urls = urls[:1]  # veille : une seule recherche, les plus récentes en candidature simplifiée
        else:
            urls.insert(1, urls[0] + "&start=25")  # passage complet : 2e page de la recherche principale
        found = {}
        tab = await browser.new_tab()
        try:
            for url in urls:  # 2 recherches au plus par passage
                await tab.goto(url)
                await tab.wait_for("() => document.querySelectorAll('[data-job-id], [data-occludable-job-id]').length"
                                   " || /login|authwall|\\/uas\\/|checkpoint/.test(location.href)", timeout=8)
                if re.search(r"login|authwall|/uas/|checkpoint", await tab.url()):
                    raise NeedLogin("LinkedIn demande de se reconnecter")
                prev = -1
                for _ in range(6):  # défilement tant que de nouvelles cartes apparaissent
                    n = await tab.eval("() => { const l = document.querySelector('.jobs-search-results-list, .scaffold-layout__list > div, .scaffold-layout__list');"
                                       " if (l) l.scrollBy(0, 1200); else window.scrollBy(0, 1200);"
                                       " return document.querySelectorAll('[data-job-id], [data-occludable-job-id]').length; }")
                    if n == prev:
                        break
                    prev = n
                    await asyncio.sleep(0.45)
                await tab.tour("[data-job-id], [data-occludable-job-id]", "Offre", 5)
                cards = await tab.eval(r"""() => { const out = {};
                  document.querySelectorAll('[data-job-id], [data-occludable-job-id]').forEach(el => {
                    const id = el.getAttribute('data-job-id') || el.getAttribute('data-occludable-job-id');
                    if (!/^\d+$/.test(id || '')) return;
                    const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(s => s);
                    if (lines.length) out[id] = {id, lines}; });
                  return Object.values(out); }""")
                for c in cards or []:
                    L = c["lines"]
                    if len(L) > 1 and L[1] == L[0]:
                        L = L[1:]
                    txt = " ".join(L)
                    found[c["id"]] = {"key": f"li:{c['id']}", "site": "linkedin", "title": L[0].replace(" avec vérification", ""),
                                      "company": L[1] if len(L) > 1 else "", "location": L[2] if len(L) > 2 else "",
                                      "url": f"https://www.linkedin.com/jobs/view/{c['id']}/", "recent": True,
                                      "already": bool(ALREADY_RE.search(txt)), "source": "LinkedIn", "card": txt[:500],
                                      "easy": bool(re.search(r"Candidature simplifiée|Easy Apply", txt))}
        finally:
            await tab.close()
        return list(found.values())

    async def details(self, tab, offer):
        await tab.goto(offer["url"])
        await tab.wait_for("() => !!document.querySelector('#job-details, .jobs-description__content, .jobs-box__html-content')", timeout=6)
        await tab.tour("#job-details p, #job-details li, .jobs-description__content p, .jobs-description__content li", "Lecture", 4)
        d = await tab.eval(r"""() => { for (const s of ['#job-details', '.jobs-description__content', '.jobs-box__html-content', 'article']) {
            const e = document.querySelector(s); if (e && e.innerText.length > 200) return e.innerText; }
            return (document.querySelector('main') || document.body).innerText; }""") or ""
        top = await tab.text("main", 3000)
        offer["description"] = d[:15000]
        offer["meta"] = top[:700]  # « il y a 2 heures · 12 candidats », « Soyez parmi les premiers… »
        offer["already"] = offer.get("already") or bool(re.search(r"Candidature envoyée|Vous avez postulé|Applied \d", top))
        offer["easy"] = await tab.eval("() => [...document.querySelectorAll('button')].some(b => /Candidature simplifiée|Easy Apply/.test(b.innerText + (b.getAttribute('aria-label') || '')))")
        return offer

    async def start(self, tab, offer):
        await tab.goto(offer["url"])
        return ("Candidature simplifiée disponible." if offer.get("easy") else
                "Pas de candidature simplifiée : « Postuler » ouvre le site de l'entreprise.")


# ------------------------------------------------------------------------------------- WTTJ
class WTTJ(SiteDef):
    async def search(self, browser, since_hours, quick=False):
        urls = [f"https://www.welcometothejungle.com/fr/jobs?query={quote(kw.replace('stage ', ''))}"
                "&refinementList%5Bcontract_type%5D%5B%5D=internship&refinementList%5Boffices.country_code%5D%5B%5D=FR&sortBy=mostRecent"
                for kw in kw_list(quick)]

        def make(c):
            o = card_offer(c, "wttj", "wttj", "Welcome to the Jungle")
            m = re.search(r"/companies/([^/]+)/", c["href"])
            if m and (not o["company"] or o["company"] == o["title"]):
                o["company"] = m.group(1).replace("-", " ").title()
            return o
        found = await search_pages(browser, urls, r"/companies/[^/]+/jobs/([^/?#]+)",
                                   "() => document.querySelectorAll('a[href*=\"/jobs/\"]').length > 3", make,
                                   tour_sel='a[href*="/companies/"][href*="/jobs/"]', next_page=lambda u, n: f"{u}&page={n}",
                                   pages=max_pages(quick))
        if not found:
            from . import db
            if db.once(f"wttj-vide:{db.today()}"):
                log("Welcome to the Jungle : aucune offre lue — sa page de recherche a changé (elle propose de créer un profil "
                    "au lieu d'afficher les résultats). Crée une alerte e-mail « stage cybersécurité » sur WTTJ : StageAuto "
                    "lit tes alertes dans Gmail et postule aux offres qu'elles contiennent.")
        return found


# --------------------------------------------------------------------------------- JobTeaser
class JobTeaser(SiteDef):
    @property
    def base(self):
        return load_settings().get("jobteaser_base", "https://www.jobteaser.com").rstrip("/")

    async def login_state(self, tab):
        if await tab.eval("() => /sign_in|login|signin|\\/auth/.test(location.href)"):
            return False, "redirigé vers la connexion JobTeaser (vérifie l'adresse de l'espace école dans Réglages)"
        return await SiteDef.login_state(self, tab)

    async def search(self, browser, since_hours, quick=False):
        urls = [f"{self.base}/fr/job-offers?contract=internship&q={quote(kw.replace('stage ', ''))}&sort=recency"
                for kw in kw_list(quick)]
        return await search_pages(browser, urls, r"/job-offers/([0-9a-f]{8}-[^/?#]+)",
                                  "() => document.querySelectorAll('a[href*=\"/job-offers/\"]').length > 2",
                                  lambda c: card_offer(c, "jobteaser", "jt", "JobTeaser"), tour_sel='a[href*="/job-offers/"]',
                                  next_page=lambda u, n: f"{u}&page={n}", pages=max_pages(quick))


# ------------------------------------------------------------------------------------ Indeed
class Indeed(SiteDef):
    async def login_state(self, tab):
        if await tab.eval("() => /just a moment|un instant/i.test(document.title) || !!document.querySelector(\"iframe[src*='challenges.cloudflare'], #challenge-form\")"):
            return False, "vérification Cloudflare à valider dans Chrome"
        return await SiteDef.login_state(self, tab)

    async def search(self, browser, since_hours, quick=False):
        out = {}
        days = 1 if since_hours <= 24 else 3
        tab = await browser.new_tab()
        try:
            for kw, page in [(kw, n) for kw in kw_list(quick) for n in range(max_pages(quick))]:
                if page and len([1 for o in out.values() if o.get("_kw") == kw]) < 10 * page:
                    continue  # la page précédente n'était pas pleine : rien de plus pour ce mot-clé
                await tab.goto(f"https://fr.indeed.com/jobs?q={quote(kw)}&l=France&fromage={days}&sort=date"
                               f"&sc=0kf%3Ajt%28internship%29%3B" + (f"&start={10 * page}" if page else ""))
                await tab.wait_for("() => document.querySelectorAll('[data-jk]').length || /just a moment|un instant/i.test(document.title)", timeout=8)
                await tab.tour("[data-jk]", "Offre", 4)
                cards = await tab.eval(r"""() => [...document.querySelectorAll('[data-jk]')].map(e => { const c = e.closest('td, li, div.job_seen_beacon') || e;
                    return {jk: e.getAttribute('data-jk'), card: (c.innerText || '').slice(0, 400), title: ((c.querySelector('h2, [class*=jobTitle]') || e).innerText || '').trim().split('\n')[0],
                    company: ((c.querySelector('[data-testid=company-name], .companyName') || {}).innerText || '').trim(),
                    location: ((c.querySelector('[data-testid=text-location], .companyLocation') || {}).innerText || '').trim()}; })""") or []
                if not cards and await tab.eval("() => /just a moment|un instant/i.test(document.title)"):
                    log("Indeed : vérification Cloudflare (connecte-toi / valide dans Chrome)")
                    break
                for c in cards:
                    if c["jk"]:
                        out.setdefault(c["jk"], {"key": f"in:{c['jk']}", "site": "indeed", "title": c["title"], "company": c["company"],
                                                 "location": c["location"], "url": f"https://fr.indeed.com/viewjob?jk={c['jk']}",
                                                 "recent": True, "source": "Indeed", "card": c.get("card", ""), "_kw": kw})
        finally:
            await tab.close()
        return list(out.values())

    async def details(self, tab, offer):
        await tab.goto(offer["url"])
        await tab.wait_for("() => !!document.querySelector('#jobDescriptionText') || /just a moment|un instant/i.test(document.title)", timeout=6)
        await tab.tour("#jobDescriptionText p, #jobDescriptionText li", "Lecture", 4)
        offer["description"] = (await tab.text("#jobDescriptionText")) or (await tab.text("body"))[:12000]
        offer["meta"] = (await tab.text("body", 1500))
        offer["already"] = bool(re.search(r"Vous avez postulé|Candidature envoyée", await tab.text("body", 3000)))
        return offer


# --------------------------------------------------------------------------------- HelloWork
class HelloWork(SiteDef):
    async def search(self, browser, since_hours, quick=False):
        d = "d" if since_hours <= 24 else "w"
        urls = [f"https://www.hellowork.com/fr-fr/emploi/recherche.html?k={quote(kw)}&l=France&c=Stage&d={d}" for kw in kw_list(quick)]
        return await search_pages(browser, urls, r"/fr-fr/emplois/(\d+)\.html",
                                  "() => document.querySelectorAll('a[href*=\"/fr-fr/emplois/\"]').length > 2",
                                  lambda c: {**card_offer(c, "hellowork", "hw", "HelloWork"), "recent": True},
                                  tour_sel='a[href*="/fr-fr/emplois/"]', next_page=lambda u, n: f"{u}&p={n}", pages=max_pages(quick))

    async def details(self, tab, offer):
        await tab.goto(offer["url"])
        await tab.wait_for("() => document.body.innerText.length > 800", timeout=6)
        await tab.tour("main p, main li", "Lecture", 4)
        t = await tab.text("body", 30000)
        offer["meta"] = t[:900]
        m = re.search(r"(Les missions du poste|Le poste|Description du poste|Missions)(.*?)(Les avantages|Postuler|Candidater|$)", t, re.S)
        offer["description"] = (m.group(0) if m else t)[:15000]
        offer["already"] = offer.get("already") or "Suivi de candidature" in t or "Vous avez déjà postulé" in t
        offer["direct"] = await tab.eval("() => !!document.querySelector('#postuler input[type=file], #postuler textarea')")
        return offer

    async def start(self, tab, offer):
        if offer.get("direct"):
            await tab.goto(offer["url"].split("#")[0] + "#postuler")
            return "Candidature directe HelloWork (formulaire #postuler en bas de page)."
        oid = offer["key"].split(":")[1]
        await tab.goto(f"https://www.hellowork.com/fr-fr/emplois/redirectionexterne.html?offerId={oid}")
        await tab.settle(6)
        return "Redirection vers le site du recruteur."


# --------------------------------------------------------------------------- SmartRecruiters
class SmartRecruiters(SiteDef):
    async def search(self, browser, since_hours, quick=False):
        tab = await browser.new_tab("https://jobs.smartrecruiters.com/")
        out = {}
        limit = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=since_hours + 6)
        empty = []
        try:
            for comp in load_settings().get("smartrecruiters_companies", []):
                total = 0
                for qq in ("stage", "intern"):
                    data = await tab.eval("async (u) => { try { const r = await fetch(u); return await r.json(); } catch (e) { return {}; } }",
                                          f"https://api.smartrecruiters.com/v1/companies/{comp}/postings?limit=100&q={qq}")
                    total += (data or {}).get("totalFound") or 0
                    for p in (data or {}).get("content", []):
                        country = ((p.get("location") or {}).get("country") or "").lower()
                        if (country and country != "fr") or not INTERN_RE.search(p.get("name") or ""):
                            continue  # stages en France seulement (la recherche renvoie aussi des CDI et l'étranger)
                        try:
                            when = dt.datetime.fromisoformat(p.get("releasedDate", "").replace("Z", "+00:00"))
                        except ValueError:
                            when = None
                        if when and when < limit:
                            continue
                        out[p["id"]] = {"key": f"sr:{p['id']}", "site": "smartrecruiters", "title": p.get("name", ""),
                                        "company": (p.get("company") or {}).get("name", comp), "sr_company": comp,
                                        "location": (p.get("location") or {}).get("city", ""), "posted": p.get("releasedDate"),
                                        "url": f"https://jobs.smartrecruiters.com/{comp}/{p['id']}", "recent": True,
                                        "source": "Site carrière"}
                if not total:
                    empty.append(comp)
        finally:
            await tab.close()
        if empty:
            from . import db
            if db.once(f"sr-vide:{db.today()}:{','.join(empty)}"):
                log(f"SmartRecruiters : aucune offre chez {', '.join(empty)} — identifiant à vérifier (Réglages → Entreprises "
                    "SmartRecruiters ; il figure dans l'adresse jobs.smartrecruiters.com/IDENTIFIANT)")
        return list(out.values())

    async def details(self, tab, offer):
        if "smartrecruiters.com" not in (tab.last_url or ""):
            await tab.goto("https://jobs.smartrecruiters.com/")
        j = await tab.eval("async (u) => { try { const r = await fetch(u); return await r.json(); } catch (e) { return {}; } }",
                           f"https://api.smartrecruiters.com/v1/companies/{offer['sr_company']}/postings/{offer['key'][3:]}") or {}
        secs = (j.get("jobAd") or {}).get("sections") or {}
        txt = "\n\n".join(re.sub(r"<[^>]+>", " ", html.unescape((v or {}).get("text", ""))) for v in secs.values())
        offer["description"] = re.sub(r"[ \t]+", " ", txt)[:15000]
        offer["url"] = j.get("postingUrl") or offer["url"]
        offer["apply_url"] = j.get("applyUrl") or ""
        return offer


# ------------------------------------------------------------------------------- orange.jobs
class Orange(SiteDef):
    async def search(self, browser, since_hours, quick=False):
        tab = await browser.new_tab("https://orange.jobs/fr/fr/search-results")
        out = []
        try:
            for frm in (0, 100):
                body = {"ddoKey": "refineSearch", "keywords": "stage", "selected_fields": {"contractType": ["Stage"]}, "size": 100,
                        "from": frm, "lang": "fr_fr", "country": "fr", "pageName": "search-results", "jobs": True,
                        "sortBy": "Most recent", "global": True}
                data = await tab.eval("""async (b) => { try { const r = await fetch('/widgets', {method: 'POST',
                    headers: {'content-type': 'application/json'}, body: JSON.stringify(b)}); return await r.json(); } catch (e) { return {}; } }""", body)
                jobs = (((data or {}).get("refineSearch") or {}).get("data") or {}).get("jobs") or []
                for j in jobs:
                    rid = j.get("reqId") or j.get("jobId")
                    out.append({"key": f"or:{rid}", "site": "orange", "title": j.get("title", ""), "company": "Orange",
                                "location": j.get("city", ""), "jobSeqNo": j.get("jobSeqNo"),
                                "url": f"https://orange.jobs/fr/fr/job/{rid}", "recent": True, "source": "orange.jobs"})
                if len(jobs) < 100:
                    break
        finally:
            await tab.close()
        return out

    async def start(self, tab, offer):
        await tab.goto(f"https://orange.jobs/fr/fr/apply?jobSeqNo={offer['jobSeqNo']}" if offer.get("jobSeqNo") else offer["url"])
        return ""


# Référence d'une offre d'après l'adresse de sa page : après une redirection (HelloWork, Indeed -> site de l'entreprise),
# on reconnaît une offre déjà connue sous un autre site (même clé que dans l'historique : or:CTS-2026-54782, th:R0344762…).
URL_KEYS = [
    (r"orange\.jobs/.*?/job/([A-Za-z]{2,6}-\d{4}-\d+)", "or:{1}"),
    (r"orange\.jobs/.*?[?&]jobSeqNo=", ""),  # numéro interne : pas de clé fiable
    (r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*?-)?(\d{6,})", "li:{1}"),
    (r"linkedin\.com/.*?[?&]currentJobId=(\d{6,})", "li:{1}"),
    (r"jobs\.smartrecruiters\.com/[^/]+/(\d{6,})", "sr:{1}"),
    (r"hellowork\.com/fr-fr/emplois/(\d+)\.html", "hw:{1}"),
    (r"indeed\.[a-z.]+/.*?[?&](?:jk|vjk)=([0-9a-f]{8,})", "in:{1}"),
    (r"jobteaser\.com/.*?/job-offers/([0-9a-f]{8}-[^/?#]+)", "jt:{1}"),
    (r"welcometothejungle\.com/.*?/companies/[^/]+/jobs/([^/?#]+)", "wttj:{1}"),
    (r"thales[^/]*\.myworkdayjobs\.com/.*?_(R\d{5,})", "th:{1}"),
    (r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com/.*?/job/.*_([A-Z]{0,4}-?\d{4,})", "wd:{1}:{2}"),
    (r"greenhouse\.io/.*?/jobs/(\d{5,})", "gh:{1}"),
    (r"[?&]gh_jid=(\d{5,})", "gh:{1}"),
    (r"jobs(?:\.eu)?\.lever\.co/[^/]+/([0-9a-f]{8}-[0-9a-f-]{27})", "lv:{1}"),
    (r"jobs\.ashbyhq\.com/[^/]+/([0-9a-f]{8}-[0-9a-f-]{27})", "ab:{1}"),
    (r"apply\.workable\.com/([A-Za-z0-9_-]+)/j/([A-Z0-9]{6,})", "wk:{1}:{2}"),
    (r"([a-z0-9-]+)\.recruitee\.com/o/([a-z0-9-]+)", "rc:{1}:{2}"),
    (r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)/job/(\d+)", "ps:{1}:{2}"),
    (r"([a-z0-9-]+)\.breezy\.hr/p/([a-z0-9-]+)", "bz:{1}:{2}"),
    (r"apec\.fr/.*?/detail-offre/([0-9]+[A-Z]?)", "ap:{1}"),
]


def key_from_url(url: str) -> str:
    for pat, fmt in URL_KEYS:
        m = re.search(pat, url or "", re.I)
        if m:
            return re.sub(r"\{(\d)\}", lambda g: m.group(int(g.group(1))).lower() if fmt.startswith(("wk", "rc", "ps", "bz", "wd"))
                          and g.group(1) == "1" else m.group(int(g.group(1))), fmt) if fmt else ""
    return ""


SITES = {
    "linkedin": LinkedIn("linkedin", "LinkedIn", "https://www.linkedin.com/feed/", "https://www.linkedin.com/login/fr"),
    "jobteaser": JobTeaser("jobteaser", "JobTeaser", "https://www.jobteaser.com/fr/job-offers", "https://www.jobteaser.com/fr/users/sign_in"),
    "wttj": WTTJ("wttj", "Welcome to the Jungle", "https://www.welcometothejungle.com/fr", "https://www.welcometothejungle.com/fr/signin"),
    "indeed": Indeed("indeed", "Indeed", "https://fr.indeed.com/", "https://secure.indeed.com/auth"),
    "hellowork": HelloWork("hellowork", "HelloWork", "https://www.hellowork.com/fr-fr/", "https://www.hellowork.com/fr-fr/connexion.html"),
    "smartrecruiters": SmartRecruiters("smartrecruiters", "SmartRecruiters (sites carrières)", "https://jobs.smartrecruiters.com/",
                                       needs_login=False),
    "orange": Orange("orange", "orange.jobs", "https://orange.jobs/fr/fr/search-results", "https://orange.jobs/fr/fr/login",
                     needs_login=False),
}
