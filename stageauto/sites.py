"""Sites d'offres : vérification de connexion, recherche des offres du jour, lecture du descriptif, point de départ de
la candidature (ensuite confiée à l'agent). Lecture par JavaScript (rapide, sans appel à Claude)."""
import asyncio
import datetime as dt
import html
import re
from dataclasses import dataclass, field
from urllib.parse import quote

from .config import load_settings
from .notify import log

LINKS_JS = r"""(pat) => { const re = new RegExp(pat); const out = {};
  document.querySelectorAll('a[href]').forEach(a => { const m = a.href.match(re); if (!m) return;
    const card = a.closest('li, article, [data-testid*=card], [class*=card], [class*=Card]') || a.parentElement || a;
    const lines = (card.innerText || a.innerText || '').split('\n').map(s => s.trim()).filter(s => s);
    if (!out[m[1]] || lines.length > out[m[1]].lines.length)
      out[m[1]] = {id: m[1], href: a.href.split('?')[0].split('#')[0], title: (a.innerText || '').trim().split('\n')[0], lines}; });
  return Object.values(out); }"""

NOT_LOGGED_BTN = r"""(words) => [...document.querySelectorAll('a, button')].some(e => e.offsetParent !== null &&
  words.some(w => (e.innerText || '').trim().toLowerCase() === w))"""

RECENT_RE = re.compile(r"(il y a \d+\s*(min|minute|heure|h\b)|aujourd|à l'instant|hier|nouveau|new|\d+\s*(h|hours?|minutes?) ago|"
                       r"today|just posted|il y a 1 j|1 day ago|publiée? (il y a|aujourd))", re.I)
ALREADY_RE = re.compile(r"Suivi de candidature|Candidature envoyée|Déjà postulé|Vous avez postulé|Applied\b|Candidaté", re.I)


def card_offer(c, site, prefix, source):
    L = [l for l in c["lines"] if len(l) < 160]
    title = c.get("title") or (L[0] if L else "")
    rest = [l for l in L if l != title]
    txt = " ".join(L)
    return {"key": f"{prefix}:{c['id']}", "site": site, "title": title, "company": rest[0] if rest else "",
            "location": rest[1] if len(rest) > 1 else "", "url": c["href"], "recent": bool(RECENT_RE.search(txt)),
            "already": bool(ALREADY_RE.search(txt)), "source": source, "card": txt[:500]}


def kw_list(quick):
    k = load_settings()["keywords"]
    return k[:3] if quick else k[:6]


async def search_pages(browser, urls, pattern, wait_js, make):
    """Ouvre chaque page de résultats, attend les cartes (au lieu d'une pause fixe) et les lit en un appel."""
    out = {}
    tab = await browser.new_tab()
    try:
        for url in urls:
            await tab.goto(url)
            await tab.wait_for(wait_js, timeout=8)
            for c in await tab.eval(LINKS_JS, pattern) or []:
                o = make(c)
                if o:
                    out[o["key"]] = o
    finally:
        await tab.close()
    return list(out.values())


@dataclass
class SiteDef:
    id: str
    name: str
    home: str
    login_url: str = ""
    needs_login: bool = True
    hint_url: str = ""
    extra: dict = field(default_factory=dict)

    async def logged_in(self, tab) -> bool:
        return True

    async def search(self, browser, since_hours, quick=False) -> list:
        return []

    async def details(self, tab, offer) -> dict:
        await tab.goto(offer["url"])
        await tab.wait_for("() => (document.querySelector('main') || document.body).innerText.length > 400", timeout=6)
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
    async def logged_in(self, tab):
        return await tab.eval(r"""() => !/login|authwall|\/uas\/|checkpoint|signup/.test(location.href) &&
            !!document.querySelector('#global-nav, header.global-nav, nav.global-nav, [data-test-global-nav], .global-nav__content')""")

    async def search(self, browser, since_hours, quick=False):
        s = load_settings()
        qs = s.get("linkedin_searches") or ["stage cybersécurité"]
        tpr = f"r{int(max(3600, since_hours * 3600))}"
        urls = [f"https://www.linkedin.com/jobs/search/?keywords={quote(qs[0])}&location=France&f_E=1&f_TPR={tpr}&sortBy=DD&f_AL=true",
                f"https://www.linkedin.com/jobs/search/?keywords={quote(qs[-1])}&location=France&f_E=1&f_TPR={tpr}&sortBy=DD"]
        if quick:
            urls = urls[:1]  # veille : une seule recherche, les plus récentes en candidature simplifiée
        found = {}
        tab = await browser.new_tab()
        try:
            for url in urls:  # 2 recherches au plus par passage
                await tab.goto(url)
                await tab.wait_for("() => document.querySelectorAll('[data-job-id], [data-occludable-job-id]').length", timeout=8)
                prev = -1
                for _ in range(6):  # défilement tant que de nouvelles cartes apparaissent
                    n = await tab.eval("() => { const l = document.querySelector('.jobs-search-results-list, .scaffold-layout__list > div, .scaffold-layout__list');"
                                       " if (l) l.scrollBy(0, 1200); else window.scrollBy(0, 1200);"
                                       " return document.querySelectorAll('[data-job-id], [data-occludable-job-id]').length; }")
                    if n == prev:
                        break
                    prev = n
                    await asyncio.sleep(0.45)
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
    async def logged_in(self, tab):
        if await tab.eval("() => !!document.querySelector(\"a[href*='/fr/me'], [data-testid*='user-menu'], [data-testid*='avatar']\")"):
            return True
        return not await tab.eval(NOT_LOGGED_BTN, ["se connecter", "connexion"])

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
        return await search_pages(browser, urls, r"/companies/[^/]+/jobs/([^/?#]+)",
                                  "() => document.querySelectorAll('a[href*=\"/jobs/\"]').length > 3", make)


# --------------------------------------------------------------------------------- JobTeaser
class JobTeaser(SiteDef):
    @property
    def base(self):
        return load_settings().get("jobteaser_base", "https://www.jobteaser.com").rstrip("/")

    async def logged_in(self, tab):
        if await tab.eval("() => /sign_in|login|signin|\\/auth/.test(location.href)"):
            return False
        return not await tab.eval(NOT_LOGGED_BTN, ["se connecter", "connexion", "log in"])

    async def search(self, browser, since_hours, quick=False):
        urls = [f"{self.base}/fr/job-offers?contract=internship&q={quote(kw.replace('stage ', ''))}&sort=recency"
                for kw in kw_list(quick)]
        return await search_pages(browser, urls, r"/job-offers/([0-9a-f]{8}-[^/?#]+)",
                                  "() => document.querySelectorAll('a[href*=\"/job-offers/\"]').length > 2",
                                  lambda c: card_offer(c, "jobteaser", "jt", "JobTeaser"))


# ------------------------------------------------------------------------------------ Indeed
class Indeed(SiteDef):
    async def logged_in(self, tab):
        snap_captcha = await tab.eval("() => /just a moment|un instant/i.test(document.title) || !!document.querySelector(\"iframe[src*='challenges.cloudflare'], #challenge-form\")")
        if snap_captcha:
            return False
        return not await tab.eval("() => [...document.querySelectorAll(\"a[href*='secure.indeed.com/auth'], a[href*='account/login']\")].some(a => a.offsetParent !== null)")

    async def search(self, browser, since_hours, quick=False):
        out = {}
        days = 1 if since_hours <= 24 else 3
        tab = await browser.new_tab()
        try:
            for kw in kw_list(quick):
                await tab.goto(f"https://fr.indeed.com/jobs?q={quote(kw)}&l=France&fromage={days}&sort=date&sc=0kf%3Ajt%28internship%29%3B")
                await tab.wait_for("() => document.querySelectorAll('[data-jk]').length || /just a moment|un instant/i.test(document.title)", timeout=8)
                cards = await tab.eval(r"""() => [...document.querySelectorAll('[data-jk]')].map(e => { const c = e.closest('td, li, div.job_seen_beacon') || e;
                    return {jk: e.getAttribute('data-jk'), card: (c.innerText || '').slice(0, 400), title: ((c.querySelector('h2, [class*=jobTitle]') || e).innerText || '').trim().split('\n')[0],
                    company: ((c.querySelector('[data-testid=company-name], .companyName') || {}).innerText || '').trim(),
                    location: ((c.querySelector('[data-testid=text-location], .companyLocation') || {}).innerText || '').trim()}; })""") or []
                if not cards and await tab.eval("() => /just a moment|un instant/i.test(document.title)"):
                    log("Indeed : vérification Cloudflare (connecte-toi / valide dans Chrome)")
                    break
                for c in cards:
                    if c["jk"]:
                        out[c["jk"]] = {"key": f"in:{c['jk']}", "site": "indeed", "title": c["title"], "company": c["company"],
                                        "location": c["location"], "url": f"https://fr.indeed.com/viewjob?jk={c['jk']}",
                                        "recent": True, "source": "Indeed", "card": c.get("card", "")}
        finally:
            await tab.close()
        return list(out.values())

    async def details(self, tab, offer):
        await tab.goto(offer["url"])
        await tab.wait_for("() => !!document.querySelector('#jobDescriptionText') || /just a moment|un instant/i.test(document.title)", timeout=6)
        offer["description"] = (await tab.text("#jobDescriptionText")) or (await tab.text("body"))[:12000]
        offer["meta"] = (await tab.text("body", 1500))
        offer["already"] = bool(re.search(r"Vous avez postulé|Candidature envoyée", await tab.text("body", 3000)))
        return offer


# --------------------------------------------------------------------------------- HelloWork
class HelloWork(SiteDef):
    async def logged_in(self, tab):
        t = await tab.text("body", 20000)
        if re.search(r"Mon espace|Mes candidatures|Déconnexion|Mon compte", t):
            return True
        return not await tab.eval(NOT_LOGGED_BTN, ["se connecter", "connexion"])

    async def search(self, browser, since_hours, quick=False):
        d = "d" if since_hours <= 24 else "w"
        urls = [f"https://www.hellowork.com/fr-fr/emploi/recherche.html?k={quote(kw)}&l=France&c=Stage&d={d}" for kw in kw_list(quick)]
        return await search_pages(browser, urls, r"/fr-fr/emplois/(\d+)\.html",
                                  "() => document.querySelectorAll('a[href*=\"/fr-fr/emplois/\"]').length > 2",
                                  lambda c: {**card_offer(c, "hellowork", "hw", "HelloWork"), "recent": True})

    async def details(self, tab, offer):
        await tab.goto(offer["url"])
        await tab.wait_for("() => document.body.innerText.length > 800", timeout=6)
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
        try:
            for comp in load_settings().get("smartrecruiters_companies", []):
                for qq in ("stage", "intern"):
                    data = await tab.eval("async (u) => { try { const r = await fetch(u); return await r.json(); } catch (e) { return {}; } }",
                                          f"https://api.smartrecruiters.com/v1/companies/{comp}/postings?limit=100&q={qq}")
                    for p in (data or {}).get("content", []):
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
        finally:
            await tab.close()
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
    async def logged_in(self, tab):
        return bool(re.search(r"Mon profil|Mon compte|Déconnexion|Mes candidatures|Sign out|My profile", await tab.text("body", 20000)))

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


SITES = {
    "linkedin": LinkedIn("linkedin", "LinkedIn", "https://www.linkedin.com/feed/", "https://www.linkedin.com/login/fr"),
    "jobteaser": JobTeaser("jobteaser", "JobTeaser", "https://www.jobteaser.com/fr/job-offers", "https://www.jobteaser.com/fr/users/sign_in"),
    "wttj": WTTJ("wttj", "Welcome to the Jungle", "https://www.welcometothejungle.com/fr", "https://www.welcometothejungle.com/fr/signin"),
    "indeed": Indeed("indeed", "Indeed", "https://fr.indeed.com/", "https://secure.indeed.com/auth"),
    "hellowork": HelloWork("hellowork", "HelloWork", "https://www.hellowork.com/fr-fr/", "https://www.hellowork.com/fr-fr/connexion.html"),
    "smartrecruiters": SmartRecruiters("smartrecruiters", "SmartRecruiters (sites carrières)", "https://jobs.smartrecruiters.com/",
                                       needs_login=False),
    "orange": Orange("orange", "orange.jobs", "https://orange.jobs/fr/fr/search-results", "https://orange.jobs/fr/fr/login"),
}
