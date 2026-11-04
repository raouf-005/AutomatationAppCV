"""Orchestration des passages.

- Passage complet (11h, 19h) : connexions, offres, candidatures, contacts LinkedIn, suivi des acceptations, Gmail,
  nouvel essai des sites reconnectés.
- Veille (toutes les ~30 min de 8h à 22h) : uniquement les nouvelles offres, pour postuler parmi les premiers.

Flux continu : les offres sont triées par priorité (récence, « soyez parmi les premiers candidats »…), lues, pré-triées
sans IA, puis chaque CV est généré et la candidature part DÈS QUE son CV est prêt, sans attendre les autres.
Deux voies en parallèle, chacune dans sa fenêtre Chrome : LinkedIn (souris humaine) et les autres sites.
"""
import asyncio
import datetime as dt
import hashlib
import itertools
import json
import re
import shutil
from pathlib import Path

from . import cdp, cvskill, db, gmail, linkedin_contacts, priority, providers, replay, sources, vault  # sources : « carrieres »
from .agent import Agent
from .config import CANDIDATURES, DOMAIN_NEG, DOMAIN_POS, WORK, load_settings
from .forms import NeedLogin, nrm
from .notify import log, push
from . import sites as sites_mod
from .sites import SITES

STATE = {"running": False, "phase": "en attente", "detail": "", "started": None, "ended": None, "connections": {},
         "connections_ts": None, "needs_login": [], "review": {}, "summary": None, "stop": False, "mode": "",
         "lanes": {}, "next_check": None, "linkedin_last_search": None, "connections_info": {}}
LANE_WINDOW = {"linkedin": 1, "web": 2, "all": 1}


def phase(p, d=""):
    STATE["phase"], STATE["detail"] = p, d
    log(f"— {p} {d}")


def window_hours(quick: bool) -> float:
    """Fenêtre de recherche : depuis la fin du passage précédent (avec un recouvrement de 15 min)."""
    r = db.q("SELECT ended FROM runs WHERE ended IS NOT NULL ORDER BY id DESC LIMIT 1")
    if r:
        try:
            h = (dt.datetime.now() - dt.datetime.fromisoformat(r[0]["ended"])).total_seconds() / 3600 + 0.25
            return min(48.0, max(1.0 if quick else 2.0, h))
        except ValueError:
            pass
    return 24.0


def in_domain(o) -> bool:
    t = nrm(o.get("title", ""))
    if any(nrm(n).strip() and re.search(r"\b" + re.escape(nrm(n).strip()) + r"\b", t) for n in DOMAIN_NEG):
        return False
    return any(nrm(p) in t for p in DOMAIN_POS) or bool(re.search(r"stage|stagiaire|intern|pfe|fin d.etudes", t))


async def check_connections(browser, ids):
    res = {}
    s = load_settings()

    async def one(sid):
        """True = connecté, False = sûrement déconnecté (site sauté), None = pas sûr (site utilisé quand même)."""
        site = SITES[sid]
        info = STATE.setdefault("connections_info", {})
        if not site.needs_login:
            res[sid], info[sid] = True, "connexion facultative"
            return
        home = site.base + "/fr/job-offers" if sid == "jobteaser" else site.home
        tab = await browser.new_tab(home)
        try:
            await tab.settle(5)
            ok, why = await site.login_state(tab)
            if ok is False and vault.get_cred(sid):
                await tab.goto(site.login_url or home)
                r = await Agent(browser, tab, sid).run(
                    f"Connecte-toi à {site.name} : repère les champs identifiant et mot de passe (read_page), utilise "
                    "fill_credentials, clique le bouton de connexion. Captcha, code par e-mail/SMS ou 2FA : finish needs_login. "
                    "Connexion réussie : finish done.", max_steps=12)
                await tab.goto(home)
                await tab.settle(5)
                ok, why = await site.login_state(tab)
                why = f"après connexion automatique ({r['status']}) : {why}"
            res[sid], info[sid] = ok, why
            await tab._cursor(*tab.pos, label=f"{site.name} : {'connecté ✓' if ok else 'non connecté' if ok is False else 'pas sûr'}")
            log(f"connexion {site.name} : {'oui' if ok else 'NON' if ok is False else 'pas sûr'} — {why}")
        except Exception as e:
            log(f"connexion {sid} : {e.__class__.__name__} {str(e)[:150]}")
            res[sid], info[sid] = None, f"vérification impossible ({e.__class__.__name__}) : site essayé quand même"
        finally:
            await tab.close()
    sem = asyncio.Semaphore(s.get("parallel_tabs", 3))

    async def guarded(sid):
        async with sem:
            await one(sid)
    await asyncio.gather(*(guarded(i) for i in ids))
    return res


async def connections(browser, ids, quick):
    """En veille, réutilise l'état des connexions vérifié il y a moins de 90 min (gain de temps)."""
    ts = STATE.get("connections_ts")
    if quick and ts and (dt.datetime.now() - ts).total_seconds() < 5400 and all(i in STATE["connections"] for i in ids):
        return {i: STATE["connections"][i] for i in ids}
    conn = await check_connections(browser, ids)
    STATE["connections"].update(conn)
    STATE["connections_ts"] = dt.datetime.now()
    return conn


def linkedin_due() -> bool:
    last = STATE.get("linkedin_last_search")
    gap = load_settings().get("linkedin_watch_min", 60)
    return last is None or (dt.datetime.now() - last).total_seconds() >= gap * 60


async def gather_offers(browser, ids, hours, quick=False):
    sem = asyncio.Semaphore(load_settings().get("parallel_tabs", 3))
    found = []

    async def one(sid):
        async with sem:
            try:
                try:
                    lst = await SITES[sid].search(browser, hours, quick=quick)
                except TypeError:  # site ajouté sans le mode veille
                    lst = await SITES[sid].search(browser, hours)
            except NeedLogin as e:  # mur de connexion pendant la recherche : vraiment déconnecté
                STATE["connections"][sid] = False
                STATE.setdefault("connections_info", {})[sid] = str(e)
                if sid not in STATE["needs_login"]:
                    STATE["needs_login"].append(sid)
                log(f"{SITES[sid].name} : {e}")
                return
            except Exception as e:
                log(f"recherche {sid} : {e.__class__.__name__} {str(e)[:100]}")
                return
            if sid == "linkedin":
                STATE["linkedin_last_search"] = dt.datetime.now()
            log(f"{SITES[sid].name} : {len(lst)} offres listées")
            db.coverage_add(sid, listed=len(lst))
            found.extend(lst)
    await asyncio.gather(*(one(i) for i in ids))
    return found


def select_new(offers):
    s = load_settings()
    keep, kept = [], []
    per_company = {}
    for o in offers:
        if o.get("already"):
            if not db.q("SELECT 1 FROM offers WHERE key=?", (o["key"],)):
                db.upsert_offer({**o, "status": "already", "reason": "déjà postulé (marqueur du site)"})
            continue
        if db.offer_seen(o["key"], o.get("company", ""), o.get("title", "")):
            continue
        # la même offre listée par deux sites dans ce passage (HelloWork + orange.jobs…) : une seule candidature
        if any(db.same_offer(o.get("company", ""), o.get("title", ""), c, t) for c, t in kept):
            continue
        kept.append((o.get("company", ""), o.get("title", "")))
        if not in_domain(o):
            db.upsert_offer({**o, "status": "ignored", "reason": "hors domaine (titre)"})
            continue
        c = db.company_key(o.get("company", "")) or nrm(o.get("company", ""))[:30]
        # entreprise encore inconnue (lien d'une alerte e-mail) : le plafond est vérifié avant l'envoi, une fois connue
        if c and per_company.get(c, 0) + db.applied_today_for(o.get("company", "")) >= s["max_per_company_per_day"]:
            continue
        if c:
            per_company[c] = per_company.get(c, 0) + 1
        priority.score(o)
        keep.append(o)
    per_source = {}
    for o in keep:
        k = o.get("source_label") or o.get("source") if o.get("site") == "carrieres" else o["site"]
        per_source[k] = per_source.get(k, 0) + 1
    for k, n in per_source.items():
        db.coverage_add(k, new=n)
    keep.sort(key=lambda o: -o["score"])
    return keep[: s.get("max_applications_per_pass", 40) * 2]


def pending_ready():
    """CV déjà prêts mais candidature pas encore envoyée (passage interrompu, plafond atteint) : repris en priorité."""
    since = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    out = []
    for r in db.q("SELECT * FROM offers WHERE status='docs_ready' AND found_at >= ?", (since,)):
        o = {**r, **json.loads(r["data"] or "{}")}
        if o.get("cv") and Path(o["cv"]).exists() and o.get("site") in SITES:
            out.append(o)
    return out


def files_for(o):
    s = load_settings()
    f = {"cv": o["cv"], "lm": o["lm"], "lettre_txt": o.get("lettre_txt", ""), "message_court": o.get("message_court", "")}
    certs = s.get("certificats_pdf")
    if certs and Path(certs).exists():
        f["certs"] = certs
    if o["site"] == "smartrecruiters":
        f["no_semicolon"] = True
    return f


def recipe_key(o, url: str) -> str:
    host = re.sub(r"^https?://([^/]+).*", r"\1", url or "").lower()
    return f"{o['site']}|{host}" + ("|easy" if o.get("easy") else "")


def key_from_seq(url: str) -> str:
    """Page de candidature orange.jobs (apply?jobSeqNo=…) : retrouve l'offre orange.jobs connue par ce numéro."""
    m = re.search(r"orange\.jobs/.*?[?&]jobSeqNo=([A-Za-z0-9]+)", url or "")
    if not m:
        return ""
    r = db.q("SELECT key FROM offers WHERE site='orange' AND data LIKE ?", (f'%"jobSeqNo": "{m.group(1)}"%',))
    return r[0]["key"] if r else ""


async def apply_one(browser, o, lane=1):
    """Méthode mémorisée d'abord (sans IA) ; l'agent prend la suite au premier imprévu ou s'il n'y a pas de méthode."""
    site = SITES[o["site"]]
    tab = await browser.new_tab(lane=lane)
    keep_open = False
    s = load_settings()
    auto = s.get("auto_submit", True)
    try:
        extra = await site.start(tab, o)
        url_now = await tab.url()
        rkey = recipe_key(o, url_now)
        if o["site"] != "carrieres":
            try:
                sources.discover(url_now, o.get("company", ""), f"redirection depuis {SITES[o['site']].name}")
            except Exception as e:
                log(f"repérage du site carrière : {e.__class__.__name__}")
        other = sites_mod.key_from_url(url_now) or key_from_seq(url_now)
        if other and other != o["key"]:
            row = db.q("SELECT status FROM offers WHERE key=?", (other,))
            if row and row[0]["status"] in ("applied", "already"):
                return {"status": "already_applied", "reason": f"redirige vers {other}, déjà postulée", "seconds": 0, "steps": 0}
        files = files_for(o)
        res, mode, t0 = None, "agent", dt.datetime.now()
        if s.get("use_memory", True):
            try:
                res = await replay.replay(browser, tab, rkey, files, o, auto=auto)
            except Exception as e:
                res = {"status": "fallback", "reason": f"{e.__class__.__name__}", "tab": tab}
            if res:
                tab = res.get("tab") or tab
                mode = "replay"
                log(f"méthode mémorisée {rkey} : {res['status']} {res.get('reason', '')}")
        if (not res or res["status"] == "fallback") and not await asyncio.to_thread(providers.ready):
            return {"status": "wait_ai", "reason": "IA en pause : candidature reprise plus tard", "seconds": 0, "steps": 0}
        if not res or res["status"] == "fallback":
            goal = (f"Postule à l'offre de stage « {o['title']} » chez {o['company']} ({o['url']}) avec le CV et la lettre "
                    f"fournis (upload cv / lettre ; texte de motivation : @@LETTRE@@ ; message court : @@MESSAGE@@). {extra} "
                    "Si le site exige de créer un compte, une vidéo ou un captcha : finish blocked/needs_login. "
                    "Succès constaté : finish applied.")
            ag = Agent(browser, tab, o["site"], files, o)
            ag.recipe_key = rkey
            if res:
                ag.extra_intro = (f"La relecture rapide de la méthode mémorisée s'est arrêtée ({res.get('reason', '')}). "
                                  "Continue depuis la page actuelle, sans recommencer ce qui est déjà rempli.")
                mode = "replay+agent"
            res = await ag.run(goal)
            tab = ag.tab or tab
            try:  # « Postuler » a pu mener au site carrière de l'entreprise : il sera surveillé directement
                sources.discover(await tab.url(), o.get("company", ""), f"candidature depuis {SITES[o['site']].name}")
            except Exception:
                pass
        else:
            res["steps"] = 0
        res["seconds"] = round((dt.datetime.now() - t0).total_seconds())
        db.record_apply(o["site"], rkey, mode, res["seconds"], res.get("steps", 0), res["status"], res.get("provider", ""))
        if mode.startswith("replay"):  # fiabilité de la méthode mémorisée
            m = db.method(rkey)
            if m.get("recipe"):
                k = "ok" if mode == "replay" and res["status"] in ("applied", "ready") else "fallbacks"
                m["recipe"][k] = m["recipe"].get(k, 0) + (1 if k == "fallbacks" or mode == "replay" else 0)
                db.save_method(rkey, m)
        if res["status"] == "ready":
            keep_open = True
            STATE["review"][o["key"]] = {"tab": tab.id, "site": o["site"], "title": o["title"], "company": o["company"]}
        return res
    finally:
        if not keep_open:
            try:
                await tab.close()
            except Exception:
                pass


def archive(o):
    d = linkedin_contacts.envoyees_dir(o["dossier"])
    for k in ("cv", "lm"):
        try:
            shutil.copy(o[k], d / Path(o[k]).name)
        except Exception:
            pass
    return str(d)


async def stream(browser, offers, summary, resume=()):
    """Lecture -> pré-tri -> CV -> candidature, offre par offre, dans l'ordre de priorité, avec deux voies d'envoi."""
    s = load_settings()
    lanes = {"linkedin": asyncio.PriorityQueue(), "web": asyncio.PriorityQueue()} if s.get("parallel_lanes", True) \
        else {"all": asyncio.PriorityQueue()}

    def lane_of(o):
        return ("linkedin" if o["site"] == "linkedin" else "web") if len(lanes) > 1 else "all"
    seq = itertools.count()
    budget = {"left": s.get("max_applications_per_pass", 40)}
    det_sem = asyncio.Semaphore(s.get("parallel_tabs", 3))
    llm_sem = asyncio.Semaphore(s.get("parallel_llm", 4))

    async def enqueue(o):
        priority.score(o)
        await lanes[lane_of(o)].put((-o["score"], next(seq), o))

    for o in resume:
        await enqueue(o)

    async def prepare(o):
        if STATE["stop"]:
            return
        async with det_sem:
            tab = await browser.new_tab()
            try:
                await SITES[o["site"]].details(tab, o)
            except Exception as e:
                log(f"détail {o['key']} : {e.__class__.__name__} {str(e)[:150]}")
            finally:
                await tab.close()
        if o.get("already"):
            db.upsert_offer({**o, "status": "already", "reason": "déjà postulé (page de l'offre)"})
            return
        if not (o.get("description") or "").strip():
            db.upsert_offer({**o, "status": "ignored", "reason": "descriptif illisible"})
            return
        if s.get("prescreen", True):
            why = priority.prescreen(o)
            if why:
                db.upsert_offer({**o, "status": "ignored", "reason": "pré-tri : " + why})
                summary["prescreened"] += 1
                return
        priority.score(o)
        async with llm_sem:
            if STATE["stop"]:
                return
            try:
                r = await asyncio.to_thread(cvskill.generate, o)
            except Exception as e:
                log(f"CV {o['company']} : {e.__class__.__name__} {str(e)[:120]}")
                db.upsert_offer({**o, "status": "error", "reason": f"génération : {e.__class__.__name__}"})
                return
        if r.get("retry"):  # l'IA n'a pas répondu (clé, crédit, réseau) : offre gardée pour le prochain passage
            db.upsert_offer({**o, "status": "pending_ai", "reason": "en attente de l'IA : " + (r.get("reason") or "")[:180]})
            summary["pending_ai"] = summary.get("pending_ai", 0) + 1
            return
        if not r.get("eligible"):
            db.upsert_offer({**o, "status": "ignored", "reason": "non éligible : " + (r.get("reason") or "")[:200]})
            return
        if not r.get("ok"):
            db.upsert_offer({**o, "status": "error", "reason": r.get("reason", "génération impossible")})
            return
        o.update({"cv": r["cv"], "lm": r["lm"], "lettre_txt": r["lettre_txt"], "message_court": r.get("message_court", ""),
                  "poste": r.get("poste"), "dossier": r["dossier"], "lang": r.get("lang"), "gen_folder": r["folder"],
                  "docs_at": db.now(), "company_ai": r.get("company") or ""})
        if r.get("company") and (not o.get("company") or sites_mod.junk_line(o["company"])):
            o["company"] = r["company"]
        dup = db.duplicate_of(o.get("company", ""), o.get("title", ""), o["key"], statuses=("applied", "already"))
        if dup:  # entreprise connue seulement maintenant : c'est une offre déjà postulée ailleurs
            db.upsert_offer({**o, "status": "already", "reason": f"déjà postulé via {dup['key']}"})
            return
        db.upsert_offer({**o, "status": "docs_ready", "reason": "; ".join(r.get("warnings", []))[:300]})
        await enqueue(o)  # la candidature part tout de suite si la voie est libre

    async def lane_worker(name):
        q = lanes[name]
        while True:
            _, _, o = await q.get()
            if o is None:
                STATE["lanes"].pop(name, None)
                return
            if STATE["stop"] or budget["left"] <= 0:
                continue  # reste « docs_ready » : repris au passage suivant
            budget["left"] -= 1
            STATE["lanes"][name] = f"{o['company']} — {o['title'][:50]}"
            try:
                await apply_and_record(browser, o, summary, LANE_WINDOW[name])
            except Exception as e:  # une erreur sur une offre n'arrête pas la voie
                log(f"voie {name} : {o['company']} : {e.__class__.__name__} {str(e)[:120]}", "error")

    workers = [asyncio.create_task(lane_worker(n)) for n in lanes]
    for o, r in zip(offers, await asyncio.gather(*(prepare(o) for o in offers), return_exceptions=True)):
        if isinstance(r, Exception):
            log(f"préparation {o.get('company')} : {r.__class__.__name__} {str(r)[:120]}", "error")
    for q in lanes.values():
        await q.put((float("inf"), next(seq), None))
    await asyncio.gather(*workers, return_exceptions=True)


async def apply_and_record(browser, o, summary, lane=1):
    s = load_settings()
    dup = db.duplicate_of(o.get("company", ""), o.get("title", ""), o["key"], statuses=("applied", "already"))
    if dup:
        db.set_status(o["key"], "already", f"déjà postulé via {dup['key']}")
        log(f"déjà postulé : {o['company']} — {o['title'][:60]} (même offre que {dup['key']}) : pas de seconde candidature")
        return
    if db.applied_today_for(o["company"]) >= s["max_per_company_per_day"]:
        db.set_status(o["key"], "ignored", "plafond d'offres par entreprise et par jour")
        return
    phase("candidatures", f"{o['company']} — {o['title'][:60]}")
    try:
        res = await apply_one(browser, o, lane)
    except Exception as e:
        res = {"status": "error", "reason": f"{e.__class__.__name__} {str(e)[:150]}"}
    st = res["status"]
    if o.get("site") == "carrieres":
        sources.note_outcome(o.get("source", ""), st, res.get("reason", ""))
    if st == "applied":
        folder = archive(o)
        db.set_status(o["key"], "applied", res.get("reason", ""), folder=folder, duration=res.get("seconds"))
        db.set_data(o["key"], verified="page", age_min=o.get("age_min"), score=o.get("score"))
        o["folder"] = folder
        summary["applied"].append(o)
        delay = ""
        try:
            delay = f" ({round((dt.datetime.now() - dt.datetime.fromisoformat(o.get('found_at') or db.now())).total_seconds() / 60)} min après détection)"
        except ValueError:
            pass
        log(f"✓ candidature envoyée : {o['company']} — {o['title'][:60]}{delay}")
    elif st == "ready":
        db.set_status(o["key"], "ready", "rempli, en attente de validation")
        summary["ready"].append(o)
    elif st == "wait_ai":  # limite d'IA, fournisseur en panne : le CV est prêt, la candidature repart plus tard
        db.set_status(o["key"], "docs_ready", res.get("reason", "en attente de l'IA")[:300])
        log(f"{o['company']} — {o['title'][:50]} : {res.get('reason', 'IA en pause')[:120]} (reprise au passage suivant)")
    elif st == "already_applied":  # postulé avant (autre site, à la main) : pas une candidature d'aujourd'hui
        db.set_status(o["key"], "already", ("déjà postulé : " + res.get("reason", ""))[:300])
    elif st == "needs_login":
        db.set_status(o["key"], "blocked_login", res.get("reason", ""))
        if o["site"] not in STATE["needs_login"]:
            STATE["needs_login"].append(o["site"])
        summary["login"].append(o)
    else:
        db.set_status(o["key"], "ignored" if st in ("blocked", "not_eligible") else "error", res.get("reason", st))
    if st in ("blocked", "not_eligible", "error", "needs_login"):
        db.set_data(o["key"], apply_failed=st, apply_failed_at=db.now())


APPLIED_MARK = re.compile(r"Candidature envoyée|Vous avez postulé|Postulé il y a|Applied \d|Application submitted|"
                          r"Votre candidature a été envoyée", re.I)


async def verify_applications(browser, applied):
    """Contrôle après envoi : LinkedIn affiche-t-il « Candidature envoyée » sur l'offre ?"""
    for o in [x for x in applied if x["site"] == "linkedin"]:
        tab = await browser.new_tab(o["url"])
        try:
            ok = await tab.wait_for("() => /Candidature envoyée|Vous avez postulé|Postulé il y a|Applied \\d|Application submitted/i"
                                    ".test((document.querySelector('main') || document.body).innerText.slice(0, 4000))", timeout=8)
            db.set_data(o["key"], verified="linkedin" if ok else "à vérifier")
            if not ok:
                log(f"⚠ LinkedIn n'affiche pas « Candidature envoyée » pour {o['company']} : à vérifier")
                o["warn"] = "non confirmé par LinkedIn"
        except Exception:
            pass
        finally:
            await tab.close()


# offres écartées volontairement (pas des échecs de candidature) : jamais proposées au réessai groupé
REJECT_RE = re.compile(r"^(non éligible|pré-tri|hors domaine|descriptif illisible|historique|déjà postulé|plafond|"
                       r"en attente de l.IA)", re.I)


def is_failed(status: str, reason: str, data: dict) -> bool:
    """Candidature tentée qui n'a pas abouti (erreur, connexion demandée, formulaire bloquant), pas une offre écartée."""
    if status in ("applied", "already", "ready", "docs_ready", "pending_ai"):
        return False
    if data.get("apply_failed"):
        return True
    if status in ("error", "blocked_login"):
        return not REJECT_RE.search(reason or "")
    return status == "ignored" and bool(reason) and not REJECT_RE.search(reason)


def failed_offers(days: int = 1) -> list:
    """Échecs des offres trouvées aujourd'hui (days=1) ou ces derniers jours."""
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    out = []
    for r in db.q("SELECT key, site, company, title, url, status, reason, data FROM offers WHERE found_at >= ? "
                  "AND status NOT IN ('applied','already','docs_ready','pending_ai','ready')", (since,)):
        d = json.loads(r.pop("data") or "{}")
        if is_failed(r["status"], r["reason"], d):
            out.append(r)
    return out


def requeue(keys) -> list:
    """Offres à réessayer : CV déjà prêt -> candidature seule ; sinon CV refait d'abord."""
    done = []
    for k in keys:
        r = db.q("SELECT data FROM offers WHERE key=?", (k,))
        if not r:
            continue
        d = json.loads(r[0]["data"] or "{}")
        ready = bool(d.get("cv") and Path(d["cv"]).exists())
        db.set_status(k, "docs_ready" if ready else "pending_ai", "réessai demandé")
        db.set_data(k, apply_failed="", login_retried=0, retry_at=db.now())
        done.append(k)
    return done


async def run_retry(keys) -> None:
    """Réessai demandé depuis le tableau de bord : seulement ces offres, sans recherche. Pas inscrit dans l'historique
    des passages : la période de recherche du passage suivant n'est pas raccourcie."""
    if STATE["running"]:
        return
    keys = set(keys)
    STATE.update(running=True, stop=False, started=db.now(), ended=None, summary=None, mode="retry", lanes={})
    summary = {"applied": [], "ready": [], "login": [], "contacts": [], "followups": [], "gmail": {}, "missing": [],
               "prescreened": 0, "mode": "retry"}
    try:
        browser = await cdp.get_browser(patient=True)
        offers = [o for o in db.pending_ai_offers(days=3) if o["key"] in keys and o.get("site") in SITES]
        resume = [o for o in pending_ready() if o["key"] in keys]
        phase("réessai", f"{len(offers) + len(resume)} offre(s) : {len(resume)} avec CV prêt, {len(offers)} à préparer")
        log(f"réessai demandé : {len(resume) + len(offers)} offre(s)")
        await stream(browser, offers, summary, resume)
        if summary["applied"]:
            phase("vérification", "confirmation des candidatures")
            await verify_applications(browser, summary["applied"])
    except Exception as e:
        log(f"réessai interrompu : {e.__class__.__name__} {e}", "error")
    finally:
        STATE["running"] = False
        STATE["ended"] = db.now()
        STATE["lanes"] = {}
        STATE["summary"] = recap(summary)
        phase("terminé", "réessai")


def gmail_due(quick: bool) -> bool:
    """Gmail : à chaque passage complet ; pendant la veille, toutes les N minutes (réglage gmail_every_min)."""
    last = STATE.get("gmail_last")
    return not quick or last is None or \
        (dt.datetime.now() - last).total_seconds() >= load_settings().get("gmail_every_min", 120) * 60


async def find_offers(browser, ids, hours, quick):
    """Recherches des sites + alertes e-mail des sites d'emploi (ce que les recherches auraient manqué)."""
    found = await gather_offers(browser, ids, hours, quick)
    if load_settings().get("gmail_alerts", True) and gmail_due(quick) and not STATE["stop"]:
        phase("alertes e-mail", "offres reçues par les alertes des sites")
        try:
            got = await gmail.alerts(browser)
            db.coverage_add("alertes e-mail", listed=len(got))
            found += got
        except Exception as e:
            log(f"alertes e-mail : {e.__class__.__name__} {str(e)[:100]}")
    return found


def track_replies(g: dict) -> int:
    """Réponses des recruteurs (entretien, refus, autre) rattachées aux candidatures ; un entretien est signalé tout de
    suite par notification, une seule fois."""
    since = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    rows = db.q("SELECT key, company, title, data FROM offers WHERE status IN ('applied','already') "
                "AND COALESCE(applied_at, found_at) >= ?", (since,))
    n = 0
    for ev in g.get("events") or []:
        tag = "reply:" + hashlib.sha1(f"{ev['from']}|{ev['subject']}".encode()).hexdigest()[:16]
        if not db.once(tag):
            continue
        hay = nrm(f"{ev['who']} {ev['from']} {ev['subject']} {ev['snippet']}")
        hit = None
        for r in rows:
            words = [w for w in nrm(r["company"]).split() if len(w) >= 3 and w not in ("group", "groupe", "france", "sas")]
            if words and words[0] in hay:
                hit = r
                break
        if hit:
            db.set_data(hit["key"], reponse=ev["kind"], reponse_le=db.now(), reponse_sujet=ev["subject"][:160],
                        reponse_lien=ev.get("link", ""))
        db.x("INSERT INTO pending(kind, site, key, note, created) VALUES('reply', ?, ?, ?, ?)",
             (ev["kind"], hit["key"] if hit else "", json.dumps({**ev, "company": hit["company"] if hit else "",
                                                                  "title": hit["title"] if hit else ""}, ensure_ascii=False), db.now()))
        who = hit["company"] if hit else ev["who"]
        box = f" (boîte {ev['account']})" if ev.get("account") else ""
        if ev["kind"] == "offre":
            push(f"Offre de stage reçue : {who}", f"{ev['subject'][:150]}{box} — réponds-leur vite")
        elif ev["kind"] == "entretien":
            push(f"Échange / entretien proposé : {who}",
                 f"{ev['subject'][:120]}{box}" + (f" — choisis un créneau : {ev['link']}" if ev.get("link") else " — propose tes disponibilités"))
        n += 1
    return n


def match_acks(acks):
    """Accusés de réception Gmail -> candidatures confirmées par e-mail."""
    since = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    n = 0
    for r in db.q("SELECT key, company, data FROM offers WHERE status='applied' AND applied_at >= ?", (since,)):
        words = [w for w in nrm(r["company"]).split() if len(w) >= 3 and w not in ("group", "groupe", "france", "sas")]
        if words and any(words[0] in nrm(a) for a in acks):
            if json.loads(r["data"] or "{}").get("verified") != "email":
                db.set_data(r["key"], verified="email")
                n += 1
    return n


async def run_pass(trigger="manuel", mode="full"):
    if STATE["running"]:
        return
    quick = mode == "quick"
    STATE.update(running=True, stop=False, started=db.now(), ended=None, summary=None, mode=mode, lanes={})
    if not quick:
        STATE["last_full_started"] = dt.datetime.now()
    if not quick:
        STATE["needs_login"] = []
    run_id = db.x("INSERT INTO runs(started) VALUES(?)", (db.now(),)).lastrowid
    summary = {"applied": [], "ready": [], "login": [], "contacts": [], "followups": [], "gmail": {}, "missing": [],
               "prescreened": 0, "mode": mode}
    try:
        db.import_seed()
        s = load_settings()
        phase("navigateur", "veille rapide" if quick else "passage complet")
        browser = await cdp.get_browser(patient=True)
        enabled = [k for k, v in s.get("sites_enabled", {}).items() if v and k in SITES]
        if quick and s.get("watch_sites"):
            enabled = [k for k in enabled if k in s["watch_sites"]]
        if "linkedin" in enabled and browser.kind != "extension" and s.get("linkedin_real_chrome_only", True):
            enabled.remove("linkedin")
            if not quick:
                push("LinkedIn ignoré", "LinkedIn passe uniquement par ton vrai Chrome : charge l'extension StageAuto Bridge et "
                                       "garde Chrome ouvert.")
        phase("connexions", ", ".join(enabled))
        conn = await connections(browser, enabled, quick)
        missing = [SITES[k].name for k, ok in conn.items() if ok is False]
        summary["missing"] = missing
        if missing and not quick:
            push("Connecte-toi à : " + ", ".join(missing),
                 "Dans Chrome (ou bouton « Se connecter » du tableau de bord) – je postule dès que c'est fait.")
        hours = 26.0 if mode == "sweep" else window_hours(quick)
        if not await asyncio.to_thread(providers.ready):  # recherche de Claude Code possible : hors de la boucle
            # sans IA (limite atteinte, pas de clé) : on cherche quand même, les offres attendent l'IA dans l'ordre de
            # priorité, et les CV déjà prêts partent si la méthode du site est connue (sans IA)
            summary["no_ai"] = True
            until = providers.next_reset()
            STATE["ai_resume_at"] = until.isoformat(timespec="minutes") if until else None
            tag = f"{db.today()} {until:%H:%M}" if until else db.today()
            if STATE.get("no_ai_alert") != tag:
                STATE["no_ai_alert"] = tag
                if until:
                    push("IA en pause (limite atteinte)", f"Reprise automatique vers {until:%H:%M}. D'ici là je continue de "
                                                          "chercher et je garde les offres. Ajoute une autre IA (Gemini, ChatGPT…) "
                                                          "dans Réglages pour continuer sans attendre.")
                else:
                    push("StageAuto attend une IA", "Ajoute une clé (Claude, ChatGPT, Gemini…) ou choisis « Claude Code » dans "
                                                    "Réglages : sans IA, je ne peux pas écrire les CV. Les offres sont gardées.")
            ok_ids = [k for k, ok in conn.items() if ok is not False]
            if quick and "linkedin" in ok_ids and not linkedin_due():
                ok_ids.remove("linkedin")
            phase("recherche (IA en pause)", f"offres des {hours:.1f} dernières heures, gardées pour l'IA")
            kept = 0
            for o in select_new(await find_offers(browser, ok_ids, hours, quick)):
                db.upsert_offer({**o, "status": "pending_ai", "reason": "en attente de l'IA"
                                 + (f" (reprise vers {until:%H:%M})" if until else "")})
                kept += 1
            summary["pending_ai"] = kept
            resume = pending_ready()
            if resume:
                phase("candidatures", f"{len(resume)} CV prêts : méthodes mémorisées seulement (IA en pause)")
                await stream(browser, [], summary, resume)
            phase("en attente d'une IA", f"reprise vers {until:%H:%M}" if until else "Réglages → Fournisseur d'IA")
            return
        STATE["ai_resume_at"] = None
        ok_ids = [k for k, ok in conn.items() if ok is not False]  # « pas sûr » : essayé quand même
        if quick and "linkedin" in ok_ids and not linkedin_due():
            ok_ids.remove("linkedin")  # LinkedIn : une recherche par heure au plus en veille
        phase("recherche", f"offres des {hours:.1f} dernières heures ({', '.join(ok_ids)})")
        offers = select_new(await find_offers(browser, ok_ids, hours, quick))
        keys = {o["key"] for o in offers}
        offers += [o for o in db.pending_ai_offers() if o["key"] not in keys and o.get("site") in SITES]  # en attente de l'IA
        resume = pending_ready()
        phase("candidatures", f"{len(offers)} nouvelles offres, {len(resume)} CV prêts en attente")
        await stream(browser, offers, summary, resume)
        if summary["applied"]:
            phase("vérification", "confirmation des candidatures")
            await verify_applications(browser, summary["applied"])
        if conn.get("linkedin") and summary["applied"]:
            for o in summary["applied"]:
                if STATE["stop"]:
                    break
                phase("contacts LinkedIn", o["company"])
                try:
                    summary["contacts"] += await linkedin_contacts.contact_for(browser, o, o.get("folder", ""))
                except Exception as e:
                    log(f"contacts {o['company']} : {e.__class__.__name__}")
        if quick and gmail_due(quick) and not STATE["stop"]:
            phase("Gmail", "réponses des recruteurs")
            summary["gmail"] = await gmail.check(browser)
            STATE["gmail_last"] = dt.datetime.now()
            track_replies(summary["gmail"])
        if not quick:
            if conn.get("linkedin"):
                phase("suivi des acceptations")
                try:
                    summary["followups"] = await linkedin_contacts.follow_ups(browser)
                except Exception as e:
                    log(f"suivi : {e.__class__.__name__}")
            phase("Gmail")
            summary["gmail"] = await gmail.check(browser)
            STATE["gmail_last"] = dt.datetime.now()
            track_replies(summary["gmail"])
            n = match_acks(summary["gmail"].get("acks") or [])
            if n:
                log(f"{n} candidature(s) confirmée(s) par un accusé de réception")
            if missing and not STATE["stop"]:
                phase("nouvel essai", "sites qui étaient déconnectés")
                again = await check_connections(browser, [k for k, ok in conn.items() if ok is False])
                STATE["connections"].update(again)
                back = [k for k, ok in again.items() if ok is not False]
                if back:
                    await stream(browser, select_new(await gather_offers(browser, back, hours)), summary)
                summary["missing"] = [SITES[k].name for k, ok in again.items() if ok is False]
            await retry_blocked_login(browser, summary)
    except Exception as e:
        log(f"passage interrompu : {e.__class__.__name__} {e}", "error")
        push("StageAuto : passage interrompu", str(e)[:200])
    finally:
        STATE["running"] = False
        STATE["ended"] = db.now()
        STATE["lanes"] = {}
        text = recap(summary)
        STATE["summary"] = text
        db.x("UPDATE runs SET ended=?, summary=? WHERE id=?", (db.now(), f"[{mode}] " + text, run_id))
        priority.refresh_recipes()
        phase("terminé", "veille" if quick else "")
        if quick:
            if summary["applied"] or summary["ready"]:
                push("StageAuto : postulé parmi les premiers", text[:900])
        elif summary["applied"] or summary["contacts"] or summary["followups"] or summary["missing"] or \
                any(summary["gmail"].get(k) for k in ("actions", "replies", "confirmed")):
            push("StageAuto : récapitulatif", text[:900])


async def retry_blocked_login(browser, summary):
    """Offres mises de côté faute de connexion : réessayées si le site est désormais connecté."""
    rows = db.q("SELECT * FROM offers WHERE status='blocked_login' AND found_at >= ?", ((dt.date.today() - dt.timedelta(days=1)).isoformat(),))
    if not rows:
        return
    ids = sorted({r["site"] for r in rows if r["site"] in SITES})
    if browser.kind != "extension" and load_settings().get("linkedin_real_chrome_only", True):
        ids = [i for i in ids if i != "linkedin"]
    sites_ok = await check_connections(browser, ids)
    side = {r["key"]: r["status"] for r in db.q("SELECT key, status FROM sources")}
    redo = []
    for r in rows:
        if sites_ok.get(r["site"]) is not False:
            o = {**r, **json.loads(r["data"] or "{}")}
            if r["site"] == "carrieres":  # site carrière : un seul nouvel essai, aucun si un compte y est demandé
                if o.get("login_retried") or side.get(o.get("source")) != "active":
                    continue
                db.set_data(r["key"], login_retried=1)
            if o.get("cv") and Path(o["cv"]).exists():
                redo.append(o)
    if redo:
        phase("nouvel essai", f"{len(redo)} offres en attente de connexion")
        await stream(browser, [], summary, resume=redo)


def recap(s) -> str:
    L = []
    if s["applied"]:
        L.append("1) Candidatures envoyées :")
        L += [f"- {o['company']} — {o.get('poste') or o['title']} — {o['url']}" + (f" ⚠ {o['warn']}" if o.get("warn") else "")
              for o in s["applied"]]
    else:
        L.append("1) Aucune nouvelle candidature envoyée.")
    if s.get("prescreened"):
        L.append(f"{s['prescreened']} offre(s) écartée(s) par le pré-tri (alternance, durée, habilitation…).")
    if s["ready"]:
        L.append("À valider dans le tableau de bord : " + ", ".join(o["company"] for o in s["ready"]))
    if s["contacts"] or s["followups"]:
        L.append("2) LinkedIn : " + "; ".join(s["contacts"] + s["followups"]))
    g = s.get("gmail") or {}
    acts = (g.get("actions") or []) + [f"réponse : {x}" for x in g.get("replies") or []]
    if acts:
        L.append("3) E-mails à traiter : " + "; ".join(acts[:8]))
    if g.get("confirmed"):
        L.append("Adresses confirmées : " + "; ".join(g["confirmed"]))
    if s.get("no_ai"):
        L.append("Aucune IA configurée : ajoute une clé ou choisis « Claude Code » dans Réglages (passage arrêté avant la recherche).")
    if s.get("pending_ai"):
        L.append(f"{s['pending_ai']} offre(s) en attente de l'IA, reprises au prochain passage.")
    if s["missing"]:
        L.append("Connexion manquante : " + ", ".join(s["missing"]))
    if s["login"]:
        L.append("En attente de connexion (réessayées au prochain passage) : " + ", ".join(o["company"] for o in s["login"]))
    return "\n".join(L)


async def submit_review(key):
    """Mode validation : envoie une candidature déjà remplie (onglet resté ouvert)."""
    r = STATE["review"].pop(key, None)
    if not r:
        return "introuvable"
    browser = await cdp.get_browser()
    tab = cdp.Tab(browser, r["tab"])
    rows = db.q("SELECT * FROM offers WHERE key=?", (key,))
    o = {**rows[0], **json.loads(rows[0]["data"] or "{}")} if rows else {}
    ag = Agent(browser, tab, r["site"], files_for(o) if o.get("cv") else {}, o)
    ag.auto = True
    res = await ag.run("Le formulaire de candidature est déjà rempli. Clique le bouton final d'envoi et constate la "
                       "confirmation : finish applied.", max_steps=8)
    if res["status"] == "applied":
        folder = archive(o) if o.get("dossier") else ""
        db.set_status(key, "applied", "validée manuellement", folder=folder)
    await tab.close()
    return res["status"]


# ------------------------------------------------------------------------- test LinkedIn
def _test_files():
    """CV et lettre de la dernière candidature envoyée, sinon CV générique compilé sans appel à Claude."""
    env = CANDIDATURES / "envoyees"
    cvs = sorted(env.glob("*/CV_*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True) if env.exists() else []
    for cv in cvs:
        lm = next(iter(sorted(cv.parent.glob("Lettre_*.pdf")) + sorted(cv.parent.glob("Cover_Letter_*.pdf"))), None)
        if lm:
            return {"cv": str(cv), "lm": str(lm), "lettre_txt": ""}
    from .gen import generator
    o = {"e": "Test", "s": "LinkedIn", "dom": "soc", "poste": "Analyste SOC", "domaine": "détection et réponse à incident",
         "equipe": "SOC", "ville": "Paris", "accroche": "Test.", "pourquoi": "Test.", "kw": [], "puces": [], "skills": [],
         "savoir": "rigueur", "preuve": "", "lang": "fr"}
    r = generator.generate(o, WORK / "test_linkedin")
    return {"cv": r["cv"], "lm": r["lm"], "lettre_txt": r.get("lettre_txt", "")} if r.get("ok") else None


async def test_linkedin():
    """Démonstration visible dans Chrome : connexion, recherche, lecture d'une offre, remplissage de la Candidature
    simplifiée par l'agent, arrêt AVANT l'envoi. Aucune candidature, invitation ni message n'est envoyé."""
    if STATE["running"]:
        return
    STATE.update(running=True, stop=False, started=db.now(), ended=None)
    try:
        phase("test LinkedIn", "ouverture de Chrome")
        browser = await cdp.get_browser(patient=True)
        if browser.kind != "extension" and load_settings().get("linkedin_real_chrome_only", True):
            push("Test LinkedIn", "Extension StageAuto Bridge non connectée : LinkedIn n'est utilisé que via ton vrai Chrome. "
                                  "Charge l'extension (chrome://extensions) puis relance.")
            return
        site = SITES["linkedin"]
        tab = await browser.new_tab(site.home)
        if browser.kind == "extension":
            try:
                await cdp.BRIDGE.call("window.show")
            except Exception:
                pass
        ok, why = await site.login_state(tab)
        log(f"LinkedIn connecté : {'oui' if ok else 'NON' if ok is False else 'pas sûr'} — {why}")
        if ok is False:
            push("Test LinkedIn", "Connecte-toi à LinkedIn dans la fenêtre Chrome ouverte, puis relance le test.")
            return
        phase("test LinkedIn", "recherche des offres des dernières 24 h")
        offers = await site.search(browser, 24)
        log(f"{len(offers)} offres trouvées :")
        for o in offers[:12]:
            log(f"  • {o['company']} — {o['title']}{' (déjà postulé)' if o.get('already') else ''}")
        cand = [o for o in offers if not o.get("already") and not db.offer_seen(o["key"], o["company"], o["title"])]
        if not cand:
            log("Aucune offre non traitée à tester.")
            return
        o = cand[0]
        phase("test LinkedIn", f"lecture de l'offre {o['company']}")
        await site.details(tab, o)
        log(f"Descriptif lu : {len(o.get('description', ''))} caractères ; candidature simplifiée : {'oui' if o.get('easy') else 'non'}")
        if not vault.get_api_key():
            log("Clé API Claude absente : ajoute-la dans Réglages pour voir l'agent remplir la candidature.")
            return
        files = await asyncio.to_thread(_test_files)
        if not files:
            log("CV de test indisponible (LaTeX non installé ?)")
            return
        phase("test LinkedIn", "l'agent remplit la candidature, SANS l'envoyer")
        ag = Agent(browser, tab, "linkedin", files, o)
        ag.auto = False  # le clic final d'envoi est bloqué
        res = await ag.run(
            f"TEST, NE RIEN ENVOYER. Sur cette offre ({o['url']}) : ouvre la Candidature simplifiée (s'il n'y en a pas : "
            "finish blocked « pas de candidature simplifiée »), remplis chaque étape (auto_fill, upload cv si demandé), "
            "avance jusqu'à l'étape « Vérifier », puis finish ready SANS cliquer « Envoyer la candidature ».", max_steps=25)
        log(f"Résultat du test : {res['status']} — {res.get('reason', '')}")
        push("Test LinkedIn terminé", f"{o['company']} : {res['status']}. Rien n'a été envoyé : ferme le dialogue "
                                      "(croix puis « Abandonner ») dans Chrome.")
    except Exception as e:
        log(f"test LinkedIn : {e.__class__.__name__} {e}", "error")
    finally:
        STATE["running"] = False
        STATE["ended"] = db.now()
        phase("terminé", "test LinkedIn")
