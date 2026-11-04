"""Sources d'offres en plus des grands sites d'emploi : des « plugins » qui ne laissent pas passer une offre du jour.

- Sites carrières des entreprises : repérés tout seuls au fil des candidatures (une offre HelloWork, Indeed ou LinkedIn
  qui renvoie vers le site de l'entreprise), ou ajoutés à la main dans l'onglet Sources. Ensuite surveillés
  directement : une offre y paraît souvent avant d'être reprise par les agrégateurs.
- Plateformes de recrutement à liste publique (Greenhouse, Lever, Ashby, Workable, Recruitee, Workday, Personio,
  Breezy) : lues en une requête, sans navigateur, avec la date de publication.
- Autres pages (site carrière maison, Taleo, Teamtailor…) et autres sites d'emploi (APEC…) : lus dans Chrome ; les
  nouvelles offres sont repérées par différence avec la visite précédente.

Ajouter une plateforme = écrire une fonction décorée par @plugin qui renvoie la liste des offres d'une source.
"""
import asyncio
import datetime as dt
import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, quote, urlparse

import httpx

from . import db
from .config import load_settings, save_settings
from .notify import log
from .sites import LINKS_JS, RECENT_RE, SITES, SiteDef, card_offer, clean_title, junk_line, kw_list

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/xml;q=0.9, */*;q=0.5", "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}

INTERN_RE = re.compile(r"\b(stages?|stagiaires?|interns?|internships?|pfe|fin d.?[ée]tudes|graduation (project|internship)|"
                       r"end[- ]of[- ]studies)\b", re.I)
FR_RE = re.compile(r"\bfrance\b|\bfr\b|paris|[iî]le[- ]de[- ]france|lyon|marseille|toulouse|nantes|bordeaux|lille|rennes|"
                   r"\bnice\b|grenoble|montpellier|strasbourg|sophia|saclay|massy|palaiseau|issy|boulogne|nanterre|courbevoie|"
                   r"la d[ée]fense|puteaux|saint[- ]denis|rueil|v[ée]lizy|guyancourt|[ée]lancourt|meudon|gennevilliers|cholet|"
                   r"brest|lannion|caen|valence|toulon|aix[- ]en|orl[ée]ans|\btours\b|\bmetz\b|nancy|dijon|reims|rouen|le havre|"
                   r"angers|limoges|clermont|besan[çc]on|\bpau\b|bayonne|cergy|versailles|montrouge|levallois|neuilly|"
                   r"ch[aâ]tillon|arcueil|ivry|vincennes|cannes|antibes|valbonne|mulhouse|annecy|chamb[ée]ry|perpignan|"
                   r"n[iî]mes|avignon|le mans|amiens|poitiers|la rochelle|lorient|vannes|quimper|saint[- ]malo|niort|troyes|"
                   r"belfort|saint[- ]quentin|plaisir|v[ée]lizy|sophia[- ]antipolis|t[ée]l[ée]travail", re.I)
ACCOUNT_RE = re.compile(r"compte|account|sign.?in|sign.?up|log.?in|connexion|s'inscrire|inscription|register|identifiant|"
                        r"mot de passe|password", re.I)

# sites d'emploi et pages techniques : jamais pris pour le site carrière d'une entreprise
NOT_SOURCES = ("linkedin.com", "indeed.com", "indeed.fr", "hellowork.com", "welcometothejungle.com", "jobteaser.com",
               "google.com", "google.fr", "gmail.com", "googleusercontent.com", "microsoftonline.com", "live.com",
               "apec.fr", "orange.jobs", "glassdoor.fr", "glassdoor.com", "monster.fr", "jobijoba.com", "meteojob.com",
               "free-work.com", "1jeune1solution.gouv.fr", "francetravail.fr", "pole-emploi.fr", "facebook.com",
               "twitter.com", "x.com", "youtube.com", "instagram.com", "localhost", "127.0.0.1", "doubleclick.net",
               "cloudflare.com", "recaptcha.net", "hcaptcha.com")
CAREER_HOST = re.compile(r"^(careers?|jobs?|emplois?|recrutement|recruitment|recrute|carrieres?|talents?|join|work|apply|"
                         r"candidats?|candidature)\.|\.(careers|jobs)$", re.I)
CAREER_PATH = re.compile(r"/(careers?|jobs?|carri[eè]res?|carriere|emplois?|offres?(-d-emploi)?|recrutement|nous-rejoindre|"
                         r"rejoignez-nous|join-us|job-offers|nos-offres|opportunit(y|ies|es)|vacanc(y|ies)|postes?)(/|$|\?)", re.I)
GENERIC_ATS = [
    (r"\.taleo\.net$", "Taleo"), (r"(successfactors|sapsf)\.(com|eu)$", "SAP SuccessFactors"), (r"\.icims\.com$", "iCIMS"),
    (r"\.talent-soft\.com$", "Talentsoft"), (r"\.digitalrecruiters\.com$", "Digitalrecruiters"),
    (r"\.jobaffinity\.fr$", "Jobaffinity"), (r"(^|\.)flatchr\.io$", "Flatchr"), (r"(^|\.)taleez\.com$", "Taleez"),
    (r"\.csod\.com$", "Cornerstone"), (r"\.oraclecloud\.com$", "Oracle"), (r"\.teamtailor\.com$", "Teamtailor"),
    (r"\.welcomekit\.co$", "Welcome Kit"), (r"(^|\.)jobvite\.com$", "Jobvite"), (r"\.bamboohr\.com$", "BambooHR"),
    (r"(^|\.)join\.com$", "JOIN"), (r"\.werecruit\.io$", "Werecruit"), (r"\.beetween\.com$", "Beetween"),
    (r"\.softgarden\.io$", "softgarden"), (r"\.applytojob\.com$", "JazzHR"), (r"\.pinpointhq\.com$", "Pinpoint"),
    (r"\.myworkdaysite\.com$", "Workday"),
]
# consignes données à l'agent selon la plateforme (formulaires connus : moins d'étapes)
HINTS = {
    "greenhouse": "Formulaire Greenhouse sur la page (ou bouton « Apply ») : prénom, nom, e-mail, téléphone, CV "
                  "(« Attach » : upload cv), lettre (upload lettre), LinkedIn, questions ; bouton « Submit Application ».",
    "lever": "Formulaire Lever « Submit your application » : CV (upload cv), nom, e-mail, téléphone, liens, lettre "
             "(texte @@LETTRE@@) ; bouton « Submit application ».",
    "ashby": "Formulaire Ashby : CV (upload cv), nom, e-mail, questions ; bouton « Submit Application ».",
    "workable": "Formulaire Workable en une page : nom, e-mail, téléphone, CV (upload cv), lettre ; « Submit application ».",
    "recruitee": "Formulaire Recruitee : nom, e-mail, téléphone, CV, lettre ; bouton « Envoyer » / « Submit ».",
    "personio": "Formulaire Personio en bas de page : nom, e-mail, CV, lettre ; « Envoyer la candidature ».",
    "breezy": "Breezy : « Apply to position » puis formulaire (nom, e-mail, téléphone, CV) ; « Submit Application ».",
    "workday": "Workday : « Postuler » puis connexion au compte Workday de l'entreprise s'il existe (jamais de création "
               "de compte : finish needs_login si aucun compte).",
}


# ------------------------------------------------------------------------------------- plugins
@dataclass
class Platform:
    id: str
    label: str
    api: bool
    fetch: Callable


PLATFORMS: dict = {}


def plugin(pid: str, label: str, api: bool = True):
    """Enregistre une plateforme : fetch(client_http, source) -> liste d'éléments (voir item())."""
    def deco(fn):
        PLATFORMS[pid] = Platform(pid, label, api, fn)
        return fn
    return deco


def item(id, title, url, location="", posted=None, intern=False, description="", apply_url="", company="", recent=False,
         card="", extra=None) -> dict:
    return {"id": str(id or ""), "title": title or "", "url": url or "", "location": location or "", "posted": posted,
            "intern": bool(intern), "description": description or "", "apply_url": apply_url or "", "company": company or "",
            "recent": recent, "card": card or "", "extra": extra or {}}


def _text(h: str) -> str:
    h = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", h or "")
    t = html.unescape(re.sub(r"<[^>]+>", " ", h))
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def _ts(v):
    """Date de publication -> datetime UTC (texte ISO, « 2026-11-04 09:00:00 UTC », millisecondes) ou None."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, dt.timezone.utc)
        s = str(v).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            s += "T00:00:00+00:00"
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


async def _json(c: httpx.AsyncClient, url: str, body=None):
    r = await (c.post(url, json=body, headers={"Content-Type": "application/json"}) if body is not None else c.get(url))
    r.raise_for_status()
    return r.json()


async def _first_ok(c, urls, body=None):
    """Premier hôte qui répond (API européenne ou mondiale) ; une erreur autre que 404 est remontée."""
    last = None
    for u in urls:
        try:
            return await _json(c, u, body)
        except httpx.HTTPStatusError as e:
            last = e
            if e.response.status_code != 404:
                raise
    raise last


@plugin("greenhouse", "Greenhouse")
async def greenhouse(c, src):
    tok, eu = src["params"]["token"], src["params"].get("eu")
    hosts = ["boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"]
    j = await _first_ok(c, [f"https://{h}/v1/boards/{tok}/jobs?content=true" for h in (hosts[::-1] if eu else hosts)])
    return [item(p.get("id"), p.get("title"), p.get("absolute_url"), (p.get("location") or {}).get("name", ""),
                 _ts(p.get("first_published") or p.get("updated_at")), description=_text(html.unescape(p.get("content") or "")))
            for p in j.get("jobs") or []]


@plugin("lever", "Lever")
async def lever(c, src):
    site, eu = src["params"]["site"], src["params"].get("eu")
    bases = ["https://api.lever.co", "https://api.eu.lever.co"]
    j = await _first_ok(c, [f"{b}/v0/postings/{site}?mode=json" for b in (bases[::-1] if eu else bases)])
    out = []
    for p in j or []:
        cat = p.get("categories") or {}
        desc = "\n".join([p.get("descriptionPlain") or ""] +
                         [f"{x.get('text', '')}\n{_text(x.get('content', ''))}" for x in p.get("lists") or []] +
                         [p.get("additionalPlain") or ""])
        out.append(item(p.get("id"), p.get("text"), p.get("hostedUrl"), cat.get("location") or ", ".join(cat.get("allLocations") or []),
                        _ts(p.get("createdAt")), bool(re.search(r"intern|stage", cat.get("commitment") or "", re.I)), desc,
                        p.get("applyUrl")))
    return out


@plugin("ashby", "Ashby")
async def ashby(c, src):
    j = await _json(c, f"https://api.ashbyhq.com/posting-api/job-board/{src['params']['org']}?includeCompensation=false")
    out = []
    for p in j.get("jobs") or []:
        if p.get("isListed") is False:
            continue
        url = p.get("jobUrl") or ""
        addr = (p.get("address") or {}).get("postalAddress") or {}
        loc = ", ".join(dict.fromkeys(x for x in [p.get("location"), addr.get("addressLocality"), addr.get("addressCountry")] if x))
        out.append(item(p.get("id") or url.rstrip("/").split("/")[-1], p.get("title"), url, loc, _ts(p.get("publishedAt")),
                        p.get("employmentType") == "Intern", p.get("descriptionPlain") or _text(p.get("descriptionHtml") or ""),
                        p.get("applyUrl")))
    return out


@plugin("workable", "Workable")
async def workable(c, src):
    acc = src["params"]["account"]
    j = await _json(c, f"https://apply.workable.com/api/v1/widget/accounts/{acc}?details=true")
    out = []
    for p in j.get("jobs") or []:
        locs = [x for x in p.get("locations") or [] if not x.get("hidden")]
        loc = ", ".join(x for x in [p.get("city"), p.get("country")] if x) or \
            "; ".join(", ".join(x for x in [l.get("city"), l.get("country")] if x) for l in locs)
        out.append(item(p.get("shortcode"), p.get("title"), f"https://apply.workable.com/{acc}/j/{p.get('shortcode')}/", loc,
                        _ts(p.get("published_on") or p.get("created_at")), bool(re.search(r"intern", p.get("employment_type") or "", re.I)),
                        _text(p.get("description") or ""), p.get("application_url"), j.get("name") or ""))
    return out


@plugin("recruitee", "Recruitee")
async def recruitee(c, src):
    co = src["params"]["company"]
    j = await _json(c, f"https://{co}.recruitee.com/api/offers/")
    return [item(p.get("slug") or p.get("id"), p.get("title"), p.get("careers_url") or f"https://{co}.recruitee.com/o/{p.get('slug')}",
                 p.get("location") or ", ".join(x for x in [p.get("city"), p.get("country")] if x),
                 _ts(p.get("published_at") or p.get("created_at")), p.get("employment_type_code") == "internship",
                 _text((p.get("description") or "") + "\n" + (p.get("requirements") or "")), p.get("careers_apply_url"))
            for p in j.get("offers") or [] if p.get("status") in (None, "published")]


def _wd_posted(t: str):
    """« Posted Today », « Publié il y a 3 jours », « Posted 30+ Days Ago » -> date approximative."""
    t = (t or "").lower()
    now = dt.datetime.now(dt.timezone.utc)
    if re.search(r"today|aujourd|heute|hoy|oggi", t):
        return now
    if re.search(r"yesterday|hier|gestern|ayer|ieri", t):
        return now - dt.timedelta(days=1)
    m = re.search(r"(\d+)", t)
    return now - dt.timedelta(days=int(m.group(1)) + (1 if "+" in t or "plus" in t else 0)) if m else None


def _wd_id(tenant: str, path: str, bullets) -> str:
    rid = path.rsplit("_", 1)[-1] if "_" in path else ((bullets or [""])[0] or hashlib.sha1(path.encode()).hexdigest()[:10])
    return rid


@plugin("workday", "Workday")
async def workday(c, src):
    P = src["params"]
    host, tenant, site = P["host"], P["tenant"], P["site"]
    base = f"https://{host}/{P['locale'] + '/' if P.get('locale') else ''}{site}"
    out, seen = [], set()
    for text in ("stage", "intern"):
        for off in range(0, 100, 20):
            j = await _json(c, f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                            {"appliedFacets": {}, "limit": 20, "offset": off, "searchText": text})
            posts = j.get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath") or ""
                if path and path not in seen:
                    seen.add(path)
                    out.append(item(_wd_id(tenant, path, p.get("bulletFields")), p.get("title"), base + path,
                                    p.get("locationsText") or "", _wd_posted(p.get("postedOn")), extra={"path": path}))
            if len(posts) < 20 or off + 20 >= (j.get("total") or 0):
                break
    return out


async def workday_description(offer: dict) -> str:
    P = offer.get("wd") or {}
    async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as c:
        j = await _json(c, f"https://{P['host']}/wday/cxs/{P['tenant']}/{P['site']}{P['path']}")
    return _text((j.get("jobPostingInfo") or {}).get("jobDescription") or "")


@plugin("personio", "Personio")
async def personio(c, src):
    co, tld = src["params"]["company"], src["params"].get("tld", "de")
    r = await c.get(f"https://{co}.jobs.personio.{tld}/xml?language=fr")
    r.raise_for_status()
    out = []
    for pos in ET.fromstring(r.content).iter("position"):
        def g(k):
            return (pos.findtext(k) or "").strip()
        desc = "\n".join(_text(v.findtext("value") or "") for v in pos.iter("jobDescription"))
        out.append(item(g("id"), g("name"), f"https://{co}.jobs.personio.{tld}/job/{g('id')}", g("office"), _ts(g("createdAt")),
                        g("employmentType").lower() in ("intern", "internship", "trainee"), desc))
    return out


@plugin("breezy", "Breezy HR")
async def breezy(c, src):
    co = src["params"]["company"]
    out = []
    for p in await _json(c, f"https://{co}.breezy.hr/json") or []:
        loc = p.get("location") or {}
        out.append(item(p.get("friendly_id") or p.get("id"), p.get("name"), p.get("url"),
                        loc.get("name") or ", ".join(x for x in [loc.get("city"), (loc.get("country") or {}).get("name")] if x),
                        _ts(p.get("published_date")), bool(re.search(r"intern", (p.get("type") or {}).get("name") or "", re.I))))
    return out


# ------------------------------------------------------------- pages lues dans Chrome (sans API)
JOBLINKS_JS = r"""() => { const out = {};
  const INTERN = /\b(stages?|stagiaires?|interns?|internships?|pfe|fin d.?[ée]tudes)\b/i;
  const JOBPATH = /(job|offre|emploi|career|carri|position|vacanc|annonce|poste|opportunit|requisition|posting)/i;
  for (const a of document.querySelectorAll('a[href]')) {
    let u; try { u = new URL(a.href, location.href); } catch (e) { continue; }
    if (!/^https?:$/.test(u.protocol)) continue;
    const t = (a.innerText || a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (t.length < 6 || t.length > 180) continue;
    if (!JOBPATH.test(u.pathname + u.search) && !INTERN.test(t)) continue;
    if (/(login|signin|sign-in|connexion|register|inscription|cookie|privacy|confidentialit|mentions|facebook|twitter|linkedin\.com|instagram)/i.test(u.href)) continue;
    const href = u.origin + u.pathname + (/[?&](id|jobid|job_id|jk|offerid|offer_id|ref|req|reqid|jobreq)=/i.test(u.search) ? u.search : '');
    if (href === location.origin + location.pathname) continue;
    const card = a.closest('li, article, tr, [class*=card], [class*=Card], [class*=job], [class*=Job], [class*=offer], [class*=result]') || a.parentElement;
    const ctx = ((card && card.innerText) || t).replace(/\s+/g, ' ').slice(0, 400);
    if (!out[href] || t.length > out[href].title.length) out[href] = {href, title: t, card: ctx};
  }
  return Object.values(out); }"""

LISTING_LINK_JS = r"""() => { const dom = h => h.split('.').slice(-2).join('.');
  const RE = /(toutes (les|nos) offres|nos offres|voir les offres|offres d.emploi|emplois|jobs|join us|rejoignez|carri[eè]res|careers|opportunit|postes ouverts|open positions|vacanc)/i;
  for (const a of document.querySelectorAll('a[href]')) {
    let u; try { u = new URL(a.href, location.href); } catch (e) { continue; }
    if (dom(u.hostname) !== dom(location.hostname) || u.href === location.href) continue;
    if (RE.test((a.innerText || '') + ' ' + (a.getAttribute('aria-label') || ''))) return u.origin + u.pathname + u.search;
  }
  return null; }"""


async def _read_links(tab, url):
    await tab.goto(url)
    await tab.wait_for("() => document.readyState === 'complete' && document.querySelectorAll('a[href]').length > 5", timeout=10)
    await tab.settle(3)
    return await tab.eval(JOBLINKS_JS) or []


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


async def check_page(browser, src):
    """Site carrière sans API : liens d'offres de la page ; sans résultat, suit une fois le lien « Offres / Jobs » et
    retient la bonne page pour les visites suivantes."""
    tab = await browser.new_tab()
    try:
        links = await _read_links(tab, src["url"])
        if not [l for l in links if INTERN_RE.search(l["title"]) or INTERN_RE.search(l["card"])]:
            nxt = await tab.eval(LISTING_LINK_JS)
            if nxt and nxt != src["url"]:
                more = await _read_links(tab, nxt)
                if more:
                    links = more
                    db.x("UPDATE sources SET url=? WHERE key=?", (nxt, src["key"]))
                    log(f"source {src['company'] or src['key']} : page des offres apprise ({nxt})")
        return [item(_sha(l["href"]), clean_title(l["title"]), l["href"], recent=bool(RECENT_RE.search(l["card"])), card=l["card"])
                for l in links]
    finally:
        await tab.close()


async def check_board(browser, src, quick=False):
    """Autre site d'emploi : sa recherche, mot-clé par mot-clé (gabarit d'adresse avec {q})."""
    P = src["params"]
    out = {}
    tab = await browser.new_tab()
    try:
        for kw in kw_list(quick):
            await tab.goto(P["template"].replace("{q}", quote(kw)))
            await tab.wait_for("() => document.readyState === 'complete' && document.querySelectorAll('a[href]').length > 5", timeout=10)
            await tab.settle(3)
            if P.get("pattern"):
                for c in await tab.eval(LINKS_JS, P["pattern"]) or []:
                    o = card_offer(c, "carrieres", P.get("prefix", "bd"), src["company"])
                    out[c["id"]] = item(c["id"], o["title"], c["href"], o["location"], intern=True, company=o["company"],
                                        recent=o["recent"], card=o["card"])
            else:
                for l in await tab.eval(JOBLINKS_JS) or []:
                    out[_sha(l["href"])] = item(_sha(l["href"]), clean_title(l["title"]), l["href"], intern=True,
                                                recent=bool(RECENT_RE.search(l["card"])), card=l["card"])
    finally:
        await tab.close()
    return list(out.values())


PLATFORMS["page"] = Platform("page", "page du site carrière (lue dans Chrome)", False, None)
PLATFORMS["board"] = Platform("board", "site d'emploi (lu dans Chrome)", False, None)
PLATFORMS["smartrecruiters"] = Platform("smartrecruiters", "SmartRecruiters", True, None)  # pris en charge par son site


# ------------------------------------------------------------------------- repérage des sources
def _host_ok(host: str) -> bool:
    return bool(host) and not any(host == h or host.endswith("." + h) for h in NOT_SOURCES)


def _listing(u) -> str:
    segs = [s for s in u.path.split("/") if s]
    for i, s in enumerate(segs):
        if CAREER_PATH.search("/" + s + "/"):
            return f"{u.scheme}://{u.netloc}/" + "/".join(segs[:i + 1])
    return f"{u.scheme}://{u.netloc}/"


def detect(url: str):
    """Plateforme et identifiant du site carrière derrière une adresse d'offre, ou None."""
    try:
        u = urlparse(url or "")
    except ValueError:
        return None
    host, segs = u.netloc.lower().split(":")[0], [s for s in u.path.split("/") if s]
    if u.scheme not in ("http", "https") or not _host_ok(host):
        return None

    def src(platform, ident, params, listing, label=""):
        return {"key": f"{platform}:{ident}", "platform": platform, "params": params, "url": listing,
                "label": label or PLATFORMS.get(platform, Platform(platform, platform, False, None)).label, "host": host}
    if re.fullmatch(r"(boards|job-boards)(\.eu)?\.greenhouse\.io", host):
        tok = (parse_qs(u.query).get("for") or [None])[0] if segs[:2] == ["embed", "job_board"] else (segs[0] if segs else None)
        if tok and tok != "embed":
            return src("greenhouse", tok.lower(), {"token": tok, "eu": ".eu." in host}, f"https://{host}/{tok}")
        return None
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and segs:
        return src("lever", segs[0].lower(), {"site": segs[0], "eu": ".eu." in host}, f"https://{host}/{segs[0]}")
    if host == "jobs.ashbyhq.com" and segs:
        return src("ashby", segs[0].lower(), {"org": segs[0]}, f"https://{host}/{segs[0]}")
    if host == "apply.workable.com" and segs and segs[0] not in ("j", "api"):
        return src("workable", segs[0].lower(), {"account": segs[0]}, f"https://{host}/{segs[0]}/")
    m = re.fullmatch(r"([a-z0-9-]+)\.recruitee\.com", host)
    if m and m.group(1) not in ("www", "app", "api"):
        return src("recruitee", m.group(1), {"company": m.group(1)}, f"https://{host}/")
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and segs:
        return src("smartrecruiters", segs[0], {"company": segs[0]}, f"https://{host}/{segs[0]}")
    m = re.fullmatch(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com", host)
    if m and segs:
        locale = segs[0] if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segs[0]) else ""
        rest = segs[1:] if locale else segs
        if rest and rest[0] not in ("wday", "job"):
            return src("workday", f"{m.group(1)}/{rest[0]}", {"host": host, "tenant": m.group(1), "site": rest[0], "locale": locale},
                       f"https://{host}/{locale + '/' if locale else ''}{rest[0]}")
        return None
    m = re.fullmatch(r"([a-z0-9-]+)\.jobs\.personio\.(de|com)", host)
    if m:
        return src("personio", m.group(1), {"company": m.group(1), "tld": m.group(2)}, f"https://{host}/")
    m = re.fullmatch(r"([a-z0-9-]+)\.breezy\.hr", host)
    if m and m.group(1) not in ("www", "app"):
        return src("breezy", m.group(1), {"company": m.group(1)}, f"https://{host}/")
    ats = next((lab for pat, lab in GENERIC_ATS if re.search(pat, host)), "")
    if ats or CAREER_HOST.search(host) or CAREER_PATH.search(u.path + "/"):
        return src("page", host, {}, _listing(u), f"{ats} (lu dans Chrome)" if ats else "")
    return None


def _company_guess(d: dict) -> str:
    ident = d["key"].split(":", 1)[1].split("/")[0]
    ident = re.sub(r"^(www|careers?|jobs?|recrutement|carrieres?)\.", "", ident)
    return ident.split(".")[0].replace("-", " ").replace("_", " ").title()


def discover(url: str, company: str = "", origin: str = "") -> dict | None:
    """Adresse vue pendant une candidature (redirection vers le site de l'entreprise…) : nouvelle source à surveiller."""
    d = detect(url)
    if not d:
        return None
    company = (company or "").strip()
    if junk_line(company):
        company = ""
    if d["platform"] == "smartrecruiters":  # le site SmartRecruiters existant s'en charge : la liste des entreprises s'allonge
        s = load_settings()
        lst = s.get("smartrecruiters_companies") or []
        if d["params"]["company"] not in lst:
            s["smartrecruiters_companies"] = lst + [d["params"]["company"]]
            save_settings(s)
            log(f"nouveau site carrière repéré : {company or d['params']['company']} (SmartRecruiters) — surveillé désormais")
            return d
        return None
    old = db.q("SELECT company, status FROM sources WHERE key=?", (d["key"],))
    if old:
        if company and not old[0]["company"]:
            db.x("UPDATE sources SET company=? WHERE key=?", (company, d["key"]))
        return None
    add(d, company or _company_guess(d), origin or "repéré pendant une candidature")
    log(f"nouveau site carrière repéré : {company or _company_guess(d)} ({d['label']}) — surveillé désormais")
    return d


def add(d: dict, company: str, origin: str, status: str = "active"):
    db.x("INSERT OR IGNORE INTO sources(key, platform, company, url, params, origin, status, added) VALUES(?,?,?,?,?,?,?,?)",
         (d["key"], d["platform"], company, d["url"], json.dumps(d["params"], ensure_ascii=False), origin, status, db.now()))


def add_manual(url: str, company: str = "") -> dict:
    """Bouton « Ajouter » de l'onglet Sources : une adresse de site carrière ou de page d'offres."""
    url = (url or "").strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    d = detect(url)
    if not d:
        u = urlparse(url)
        if not _host_ok(u.netloc.lower()):
            return {"ok": False, "error": "c'est un grand site d'emploi déjà couvert, pas un site carrière d'entreprise"}
        d = {"key": f"page:{u.netloc.lower()}", "platform": "page", "params": {}, "url": url, "label": "page", "host": u.netloc}
    if d["platform"] == "page":
        d["url"] = url  # l'adresse donnée est la page des offres
    if d["platform"] == "smartrecruiters":
        discover(url, company, "ajouté à la main")
        return {"ok": True, "key": d["key"], "label": "SmartRecruiters (ajouté à la liste du site SmartRecruiters)"}
    if db.q("SELECT 1 FROM sources WHERE key=?", (d["key"],)):
        db.x("UPDATE sources SET status='active', url=CASE WHEN platform='page' THEN ? ELSE url END WHERE key=?", (d["url"], d["key"]))
    else:
        add(d, company or _company_guess(d), "ajouté à la main")
    return {"ok": True, "key": d["key"], "label": d["label"]}


# autres sites d'emploi connus : essayés une fois, gardés s'ils renvoient des offres (sinon mis de côté tout seuls)
BOARDS = [
    {"key": "board:apec", "company": "APEC", "template": "https://www.apec.fr/candidat/recherche-emploi.html/emploi?motsCles={q}",
     "pattern": r"/detail-offre/([0-9]+[A-Z]?)", "prefix": "ap"},
    {"key": "board:1jeune1solution", "company": "1jeune1solution (stages)",
     "template": "https://www.1jeune1solution.gouv.fr/stages?motCle={q}", "pattern": r"/stages/([a-z0-9][a-z0-9-]{8,})", "prefix": "1j1s"},
    {"key": "board:free-work", "company": "Free-Work (IT)",
     "template": "https://www.free-work.com/fr/tech-it/jobs?query={q}&contracts=internship", "pattern": r"/job-mission/([a-z0-9-]+)",
     "prefix": "fw"},
]


def setup():
    """Au démarrage : site « Sites carrières » activé, autres sites d'emploi proposés (une seule fois)."""
    s = load_settings()
    en = s.get("sites_enabled") or {}
    if "carrieres" not in en:
        en["carrieres"] = True
        s["sites_enabled"] = en
        save_settings(s)
    if not db.q("SELECT 1 FROM methods WHERE site='_boards_seed'"):
        for b in BOARDS:
            add({"key": b["key"], "platform": "board", "url": b["template"],
                 "params": {k: b[k] for k in ("template", "pattern", "prefix")}}, b["company"], "site d'emploi connu", "à vérifier")
        db.x("INSERT OR REPLACE INTO methods(site, data) VALUES('_boards_seed', '{}')")


# --------------------------------------------------------------------------------- vérification
def all_sources() -> list:
    out = []
    for r in db.q("SELECT * FROM sources ORDER BY added"):
        r["params"] = json.loads(r["params"] or "{}")
        out.append(r)
    return out


def fr_ok(loc: str) -> bool:
    return not (loc or "").strip() or bool(FR_RE.search(loc))


def _key(src: dict, it: dict) -> str:
    p, P = src["platform"], src["params"]
    if p == "workday":
        return f"th:{it['id']}" if P.get("tenant") == "thales" else f"wd:{P.get('tenant')}:{it['id']}"
    if p == "board":
        from .sites import key_from_url
        return key_from_url(it["url"]) or f"{P.get('prefix', 'bd')}:{it['id']}"
    if p in ("greenhouse", "lever", "ashby"):  # identifiants uniques sur toute la plateforme (comme dans les adresses)
        return f"{ {'greenhouse': 'gh', 'lever': 'lv', 'ashby': 'ab'}[p] }:{it['id']}"
    pre = {"workable": "wk", "recruitee": "rc", "personio": "ps", "breezy": "bz", "page": "pg"}.get(p, p[:3])
    return f"{pre}:{src['key'].split(':', 1)[1]}:{it['id']}"


def build(src: dict, items: list, hours: float, first: bool, dry: bool = False) -> tuple:
    """Éléments d'une source -> offres du jour (stage, en France, publiées dans la fenêtre ou jamais vues). Renvoie
    (offres, statistiques)."""
    now = dt.datetime.now(dt.timezone.utc)
    seen = {r["item"] for r in db.q("SELECT item FROM source_seen WHERE source=?", (src["key"],))}
    st = {"items": len(items), "stages": 0, "france": 0, "du_jour": 0, "nouvelles": 0}
    out, new_seen = [], []
    label = PLATFORMS.get(src["platform"], Platform("", src["platform"], False, None)).label
    for it in items:
        title = clean_title(it["title"])
        if not title or junk_line(title) or not it["url"]:
            continue
        if not (it["intern"] or INTERN_RE.search(title)):
            continue
        st["stages"] += 1
        if not fr_ok(it["location"]):
            continue
        st["france"] += 1
        key = _key(src, it)
        was_seen = key in seen
        new_seen.append(key)
        ts = it["posted"]
        if ts is not None:
            if ts < now - dt.timedelta(hours=hours):
                continue
        elif first and not it["recent"]:
            continue  # première visite d'une page sans dates : seules les offres marquées « récentes » comptent
        st["du_jour"] += 1
        if was_seen and not dry:  # bouton « Tester » : les offres du jour sont montrées même déjà vues
            continue
        st["nouvelles"] += not was_seen
        o = {"key": key, "site": "carrieres", "source": src["key"], "platform": src["platform"],
             "source_label": f"{src['company']} ({label})", "title": title,
             "company": it["company"] or src["company"] or "", "location": it["location"], "url": it["url"],
             "apply_url": it["apply_url"], "posted": ts.isoformat(timespec="minutes") if ts else "", "recent": True,
             "description": it["description"][:15000], "card": it["card"][:500]}
        if src["platform"] == "workday":
            o["wd"] = {"host": src["params"]["host"], "tenant": src["params"]["tenant"], "site": src["params"]["site"],
                       "path": it["extra"].get("path", "")}
        out.append(o)
    if not dry:
        with db._lock:
            db._conn.executemany("INSERT OR IGNORE INTO source_seen(source, item, first_seen) VALUES(?,?,?)",
                                 [(src["key"], k, db.now()) for k in new_seen])
            db._conn.commit()
    return out, st


async def check(client, browser, src: dict, since_hours: float, quick: bool = False, dry: bool = False):
    """Une source : (offres nouvelles, statistiques). Met à jour son état (dernière visite, échecs, résultats)."""
    first = not src.get("checks")
    hours = 24.0
    if not first and src.get("last_ok"):
        try:
            gap = (dt.datetime.now() - dt.datetime.fromisoformat(src["last_ok"])).total_seconds() / 3600 + 1
            hours = min(48.0, max(since_hours, gap))
        except ValueError:
            pass
    p = src["platform"]
    try:
        if p == "board":
            items = await check_board(browser, src, quick)
        elif p == "page":
            items = await check_page(browser, src)
        else:
            items = await PLATFORMS[p].fetch(client, src)
        offers, st = build(src, items, hours, first, dry)
    except Exception as e:
        err = f"{e.__class__.__name__} {str(e)[:160]}"
        fails = (src.get("fails") or 0) + 1
        status = src["status"]
        if status == "à vérifier" and fails >= 2:
            status = "sans résultat"
        elif status == "active" and fails >= 5:
            status = "en échec"
            log(f"source {src['company']} : 5 échecs de suite ({err}) — mise de côté, réactivable dans Sources")
        db.x("UPDATE sources SET last_check=?, last_error=?, fails=?, status=? WHERE key=?", (db.now(), err, fails, status, src["key"]))
        return [], {"error": err}
    status = src["status"]
    fails = 0 if st["items"] else (src.get("fails") or 0) + (1 if p in ("page", "board") else 0)
    if status == "à vérifier":  # site d'emploi proposé : gardé s'il renvoie des offres de stage, sinon mis de côté
        status = "active" if st["stages"] else ("sans résultat" if fails >= 2 else status)
        if status == "active" and not dry:
            log(f"nouveau site d'offres validé : {src['company']} ({st['stages']} offres de stage lues)")
    if not dry:
        db.x("UPDATE sources SET last_check=?, last_ok=?, last_error=?, fails=?, checks=checks+1, status=?, "
             "found_total=found_total+?, last_found=? WHERE key=?",
             (db.now(), db.now(), "" if st["items"] or p not in ("page", "board") else "aucun lien d'offre sur la page",
              fails, status, len(offers), len(offers), src["key"]))
    return offers, st


async def search(browser, since_hours: float, quick: bool = False) -> list:
    """Toutes les sources actives : plateformes à API à chaque passage (rapide), pages et autres sites d'emploi lors
    des passages complets (les moins récemment vues d'abord ; toutes au rattrapage du soir)."""
    rows = [s for s in all_sources() if s["status"] in ("active", "à vérifier")]
    api = [s for s in rows if s["platform"] not in ("page", "board", "smartrecruiters") and s["platform"] in PLATFORMS]
    web = sorted([s for s in rows if s["platform"] in ("page", "board")], key=lambda s: s.get("last_check") or "")
    if quick:
        web = []
    elif since_hours < 24:
        web = web[: int(load_settings().get("sources_pages_per_pass", 12))]
    out = []
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as c:
        async def one(s):
            async with sem:
                offers, st = await check(c, browser, s, since_hours, quick)
                out.extend(offers)
        await asyncio.gather(*(one(s) for s in api))
        for s in web:
            offers, st = await check(c, browser, s, since_hours, quick)
            out.extend(offers)
    if rows:
        log(f"sites carrières et autres sites : {len(api)} lus par API, {len(web)} dans Chrome, {len(out)} offres nouvelles")
    return out


async def test(key: str, browser=None) -> dict:
    """Bouton « Tester » : lit la source sans rien marquer comme vu (les offres restent nouvelles pour le passage)."""
    src = next((s for s in all_sources() if s["key"] == key), None)
    if not src:
        return {"ok": False, "error": "source inconnue"}
    if src["platform"] in ("page", "board") and browser is None:
        return {"ok": False, "error": "cette source se lit dans Chrome : l'extension doit être connectée"}
    async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as c:
        offers, st = await check(c, browser, dict(src, checks=src.get("checks") or 1), 24.0, dry=True)
    if st.get("error"):
        return {"ok": False, "error": st["error"]}
    return {"ok": True, **st, "exemples": [o["title"] for o in offers[:5]]}


def note_outcome(source_key: str, status: str, reason: str):
    """Candidature arrêtée par un compte à créer ou une connexion : au bout de 2 fois, le site est mis de côté."""
    if not source_key or status not in ("needs_login", "blocked") or not ACCOUNT_RE.search(reason or ""):
        return
    db.x("UPDATE sources SET account_blocks=account_blocks+1 WHERE key=?", (source_key,))
    r = db.q("SELECT company, account_blocks FROM sources WHERE key=?", (source_key,))
    if r and r[0]["account_blocks"] >= 2:
        db.x("UPDATE sources SET status='compte requis' WHERE key=?", (source_key,))
        log(f"source {r[0]['company']} : un compte est demandé pour postuler — mise de côté (réactivable dans Sources "
            "une fois connecté)")


# ------------------------------------------------------------------------- le « site » des sources
class CareerSites(SiteDef):
    async def search(self, browser, since_hours, quick=False):
        return await search(browser, since_hours, quick)

    async def details(self, tab, offer):
        if offer.get("platform") == "workday" and not offer.get("description") and offer.get("wd", {}).get("path"):
            try:
                offer["description"] = await workday_description(offer)
            except Exception as e:
                log(f"description Workday : {e.__class__.__name__}")
        if len(offer.get("description") or "") > 300:  # description fournie par l'API : pas besoin d'ouvrir la page
            offer["meta"] = (f"publiée le {offer['posted'][:16].replace('T', ' ')} · " if offer.get("posted") else "") + \
                offer["description"][:600]
            offer.setdefault("already", False)
            return offer
        return await SiteDef.details(self, tab, offer)

    async def start(self, tab, offer):
        await tab.goto(offer.get("apply_url") or offer["url"])
        return HINTS.get(offer.get("platform"), "Site carrière de l'entreprise : bouton « Postuler » / « Apply », puis le "
                                                "formulaire (jamais de création de compte).")


SITES["carrieres"] = CareerSites("carrieres", "Sites carrières et autres sites", "about:blank", needs_login=False)
