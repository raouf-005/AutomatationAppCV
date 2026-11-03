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
import itertools
import json
import re
import shutil
from pathlib import Path

from . import cdp, cvskill, db, gmail, linkedin_contacts, priority, replay, vault
from .agent import Agent
from .config import CANDIDATURES, DOMAIN_NEG, DOMAIN_POS, WORK, load_settings
from .forms import nrm
from .notify import log, push
from .sites import SITES

STATE = {"running": False, "phase": "en attente", "detail": "", "started": None, "ended": None, "connections": {},
         "connections_ts": None, "needs_login": [], "review": {}, "summary": None, "stop": False, "mode": "",
         "lanes": {}, "next_check": None, "linkedin_last_search": None}
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
        site = SITES[sid]
        if not site.needs_login:
            res[sid] = True
            return
        home = site.base + "/fr/job-offers" if sid == "jobteaser" else site.home
        tab = await browser.new_tab(home)
        try:
            await tab.settle(5)
            ok = await site.logged_in(tab)
            if not ok and vault.get_cred(sid):
                await tab.goto(site.login_url or home)
                r = await Agent(browser, tab, sid).run(
                    f"Connecte-toi à {site.name} : repère les champs identifiant et mot de passe (read_page), utilise "
                    "fill_credentials, clique le bouton de connexion. Captcha, code par e-mail/SMS ou 2FA : finish needs_login. "
                    "Connexion réussie : finish done.", max_steps=12)
                await tab.goto(home)
                ok = r["status"] == "done" and await site.logged_in(tab)
            res[sid] = bool(ok)
        except Exception as e:
            log(f"connexion {sid} : {e.__class__.__name__}")
            res[sid] = False
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
                if sid == "linkedin":
                    STATE["linkedin_last_search"] = dt.datetime.now()
                log(f"{SITES[sid].name} : {len(lst)} offres listées")
                found.extend(lst)
            except Exception as e:
                log(f"recherche {sid} : {e.__class__.__name__} {str(e)[:100]}")
    await asyncio.gather(*(one(i) for i in ids))
    return found


def select_new(offers):
    s = load_settings()
    keep, seen_norm = [], set()
    per_company = {}
    for o in offers:
        if o.get("already"):
            if not db.q("SELECT 1 FROM offers WHERE key=?", (o["key"],)):
                db.upsert_offer({**o, "status": "applied", "reason": "déjà postulé (marqueur du site)"})
            continue
        if db.offer_seen(o["key"], o.get("company", ""), o.get("title", "")):
            continue
        n = db.offer_norm(o.get("company", ""), o.get("title", ""))
        if n in seen_norm:
            continue
        seen_norm.add(n)
        if not in_domain(o):
            db.upsert_offer({**o, "status": "ignored", "reason": "hors domaine (titre)"})
            continue
        c = nrm(o.get("company", ""))[:30]
        if per_company.get(c, 0) + db.applied_today_for(o.get("company", "")) >= s["max_per_company_per_day"]:
            continue
        per_company[c] = per_company.get(c, 0) + 1
        priority.score(o)
        keep.append(o)
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


async def apply_one(browser, o, lane=1):
    """Méthode mémorisée d'abord (sans IA) ; l'agent prend la suite au premier imprévu ou s'il n'y a pas de méthode."""
    site = SITES[o["site"]]
    tab = await browser.new_tab(lane=lane)
    keep_open = False
    s = load_settings()
    auto = s.get("auto_submit", True)
    try:
        extra = await site.start(tab, o)
        rkey = recipe_key(o, await tab.url())
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
                log(f"détail {o['key']} : {e.__class__.__name__}")
            finally:
                await tab.close()
        if o.get("already"):
            db.upsert_offer({**o, "status": "applied", "reason": "déjà postulé (page de l'offre)"})
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
        if not r.get("eligible"):
            db.upsert_offer({**o, "status": "ignored", "reason": "non éligible : " + (r.get("reason") or "")[:200]})
            return
        if not r.get("ok"):
            db.upsert_offer({**o, "status": "error", "reason": r.get("reason", "génération impossible")})
            return
        o.update({"cv": r["cv"], "lm": r["lm"], "lettre_txt": r["lettre_txt"], "message_court": r.get("message_court", ""),
                  "poste": r.get("poste"), "dossier": r["dossier"], "lang": r.get("lang"), "gen_folder": r["folder"],
                  "docs_at": db.now()})
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
    if db.applied_today_for(o["company"]) >= s["max_per_company_per_day"]:
        db.set_status(o["key"], "ignored", "plafond d'offres par entreprise et par jour")
        return
    phase("candidatures", f"{o['company']} — {o['title'][:60]}")
    try:
        res = await apply_one(browser, o, lane)
    except Exception as e:
        res = {"status": "error", "reason": f"{e.__class__.__name__} {str(e)[:150]}"}
    st = res["status"]
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
    elif st == "already_applied":
        db.set_status(o["key"], "applied", "déjà postulé")
    elif st == "needs_login":
        db.set_status(o["key"], "blocked_login", res.get("reason", ""))
        if o["site"] not in STATE["needs_login"]:
            STATE["needs_login"].append(o["site"])
        summary["login"].append(o)
    else:
        db.set_status(o["key"], "ignored" if st in ("blocked", "not_eligible") else "error", res.get("reason", st))


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
        missing = [SITES[k].name for k, ok in conn.items() if not ok]
        summary["missing"] = missing
        if missing and not quick:
            push("Connecte-toi à : " + ", ".join(missing),
                 "Dans Chrome (ou bouton « Se connecter » du tableau de bord) – je postule dès que c'est fait.")
        hours = window_hours(quick)
        ok_ids = [k for k, ok in conn.items() if ok]
        if quick and "linkedin" in ok_ids and not linkedin_due():
            ok_ids.remove("linkedin")  # LinkedIn : une recherche par heure au plus en veille
        phase("recherche", f"offres des {hours:.1f} dernières heures ({', '.join(ok_ids)})")
        offers = select_new(await gather_offers(browser, ok_ids, hours, quick))
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
        if not quick:
            if conn.get("linkedin"):
                phase("suivi des acceptations")
                try:
                    summary["followups"] = await linkedin_contacts.follow_ups(browser)
                except Exception as e:
                    log(f"suivi : {e.__class__.__name__}")
            phase("Gmail")
            summary["gmail"] = await gmail.check(browser)
            n = match_acks(summary["gmail"].get("acks") or [])
            if n:
                log(f"{n} candidature(s) confirmée(s) par un accusé de réception")
            if missing and not STATE["stop"]:
                phase("nouvel essai", "sites qui étaient déconnectés")
                again = await check_connections(browser, [k for k, ok in conn.items() if not ok])
                STATE["connections"].update(again)
                back = [k for k, ok in again.items() if ok]
                if back:
                    await stream(browser, select_new(await gather_offers(browser, back, hours)), summary)
                summary["missing"] = [SITES[k].name for k, ok in again.items() if not ok]
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
    redo = []
    for r in rows:
        if sites_ok.get(r["site"]):
            o = {**r, **json.loads(r["data"] or "{}")}
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
        ok = await site.logged_in(tab)
        log(f"LinkedIn connecté : {'oui' if ok else 'NON'}")
        if not ok:
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
