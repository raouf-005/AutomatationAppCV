"""Un passage complet : connexions -> offres du jour -> CV/lettres en lot -> candidatures -> contacts LinkedIn ->
suivi des acceptations -> Gmail -> nouvel essai des sites reconnectés -> récapitulatif."""
import asyncio
import datetime as dt
import json
import re
import shutil
from pathlib import Path

from . import cdp, cvskill, db, gmail, linkedin_contacts, replay, vault
from .agent import Agent
from .config import CANDIDATURES, DOMAIN_NEG, DOMAIN_POS, WORK, load_settings
from .forms import nrm
from .notify import log, push
from .sites import SITES

STATE = {"running": False, "phase": "en attente", "detail": "", "started": None, "ended": None, "connections": {},
         "needs_login": [], "review": {}, "summary": None, "stop": False}


def phase(p, d=""):
    STATE["phase"], STATE["detail"] = p, d
    log(f"— {p} {d}")


def since_hours() -> int:
    r = db.q("SELECT ended FROM runs WHERE ended IS NOT NULL ORDER BY id DESC LIMIT 1")
    if r:
        try:
            h = (dt.datetime.now() - dt.datetime.fromisoformat(r[0]["ended"])).total_seconds() / 3600
            return int(min(48, max(2, h + 1)))
        except ValueError:
            pass
    return 24


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
            await asyncio.sleep(1.5)
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
    sem = asyncio.Semaphore(s.get("parallel_tabs", 4))

    async def guarded(sid):
        async with sem:
            await one(sid)
    await asyncio.gather(*(guarded(i) for i in ids))
    return res


async def gather_offers(browser, ids, hours):
    sem = asyncio.Semaphore(load_settings().get("parallel_tabs", 4))
    found = []

    async def one(sid):
        async with sem:
            try:
                lst = await SITES[sid].search(browser, hours)
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
        keep.append(o)
    return keep[: s.get("max_applications_per_pass", 40) * 2]


async def read_details(browser, offers):
    sem = asyncio.Semaphore(load_settings().get("parallel_tabs", 4))

    async def one(o):
        async with sem:
            tab = await browser.new_tab()
            try:
                await SITES[o["site"]].details(tab, o)
            except Exception as e:
                log(f"détail {o['key']} : {e.__class__.__name__}")
            finally:
                await tab.close()
    await asyncio.gather(*(one(o) for o in offers))
    out = []
    for o in offers:
        if o.get("already"):
            db.upsert_offer({**o, "status": "applied", "reason": "déjà postulé (page de l'offre)"})
        elif not (o.get("description") or "").strip():
            db.upsert_offer({**o, "status": "ignored", "reason": "descriptif illisible"})
        else:
            out.append(o)
    return out


async def generate_all(offers):
    sem = asyncio.Semaphore(load_settings().get("parallel_llm", 4))
    ready = []

    async def one(o):
        async with sem:
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
                      "poste": r.get("poste"), "dossier": r["dossier"], "lang": r.get("lang"), "gen_folder": r["folder"]})
            if r.get("company"):
                o["company_clean"] = r["company"]
            db.upsert_offer({**o, "status": "docs_ready", "reason": "; ".join(r.get("warnings", []))[:300]})
            ready.append(o)
    await asyncio.gather(*(one(o) for o in offers))
    return ready


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


async def apply_one(browser, o):
    """Méthode mémorisée d'abord (sans IA) ; l'agent prend la suite au premier imprévu ou s'il n'y a pas de méthode."""
    site = SITES[o["site"]]
    tab = await browser.new_tab()
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


async def apply_all(browser, offers, summary):
    s = load_settings()
    for o in offers[: s.get("max_applications_per_pass", 40)]:
        if STATE["stop"]:
            break
        if db.applied_today_for(o["company"]) >= s["max_per_company_per_day"]:
            db.set_status(o["key"], "ignored", "plafond 3 offres/entreprise/jour")
            continue
        phase("candidatures", f"{o['company']} — {o['title'][:60]}")
        try:
            res = await apply_one(browser, o)
        except Exception as e:
            res = {"status": "error", "reason": f"{e.__class__.__name__} {str(e)[:150]}"}
        st = res["status"]
        if st == "applied":
            folder = archive(o)
            db.set_status(o["key"], "applied", res.get("reason", ""), folder=folder, duration=res.get("seconds"))
            o["folder"] = folder
            summary["applied"].append(o)
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


async def run_pass(trigger="manuel"):
    if STATE["running"]:
        return
    STATE.update(running=True, stop=False, started=db.now(), ended=None, needs_login=[], summary=None)
    run_id = db.x("INSERT INTO runs(started) VALUES(?)", (db.now(),)).lastrowid
    summary = {"applied": [], "ready": [], "login": [], "contacts": [], "followups": [], "gmail": {}, "missing": []}
    try:
        db.import_seed()
        s = load_settings()
        phase("navigateur", "connexion à Chrome")
        browser = await cdp.get_browser()
        enabled = [k for k, v in s.get("sites_enabled", {}).items() if v and k in SITES]
        if "linkedin" in enabled and browser.kind != "extension" and s.get("linkedin_real_chrome_only", True):
            enabled.remove("linkedin")
            push("LinkedIn ignoré", "LinkedIn passe uniquement par ton vrai Chrome : charge l'extension StageAuto Bridge et "
                                   "garde Chrome ouvert.")
        phase("connexions", ", ".join(enabled))
        conn = await check_connections(browser, enabled)
        STATE["connections"] = conn
        missing = [SITES[k].name for k, ok in conn.items() if not ok]
        summary["missing"] = missing
        if missing:
            push("Connecte-toi à : " + ", ".join(missing),
                 "Dans Chrome (ou bouton « Se connecter » du tableau de bord) – je postule dès que c'est fait.")
        hours = since_hours()
        ok_ids = [k for k, ok in conn.items() if ok]
        phase("recherche", f"offres des {hours} dernières heures")
        offers = select_new(await gather_offers(browser, ok_ids, hours))
        phase("lecture des offres", f"{len(offers)} nouvelles")
        offers = await read_details(browser, offers)
        phase("CV et lettres", f"{len(offers)} offres (skill candidature-stage-ciblee)")
        ready = await generate_all(offers)
        phase("candidatures", f"{len(ready)} prêtes")
        await apply_all(browser, ready, summary)
        if conn.get("linkedin"):
            for o in summary["applied"]:
                if STATE["stop"]:
                    break
                phase("contacts LinkedIn", o["company"])
                try:
                    summary["contacts"] += await linkedin_contacts.contact_for(browser, o, o.get("folder", ""))
                except Exception as e:
                    log(f"contacts {o['company']} : {e.__class__.__name__}")
            phase("suivi des acceptations")
            try:
                summary["followups"] = await linkedin_contacts.follow_ups(browser)
            except Exception as e:
                log(f"suivi : {e.__class__.__name__}")
        phase("Gmail")
        summary["gmail"] = await gmail.check(browser)
        # sites reconnectés pendant le passage : on recommence pour eux
        if missing and not STATE["stop"]:
            phase("nouvel essai", "sites qui étaient déconnectés")
            again = await check_connections(browser, [k for k, ok in conn.items() if not ok])
            back = [k for k, ok in again.items() if ok]
            if back:
                STATE["connections"].update(again)
                more = await generate_all(await read_details(browser, select_new(await gather_offers(browser, back, hours))))
                await apply_all(browser, more, summary)
            summary["missing"] = [SITES[k].name for k, ok in again.items() if not ok]
        await retry_blocked_login(browser, summary)
    except Exception as e:
        log(f"passage interrompu : {e.__class__.__name__} {e}", "error")
        push("StageAuto : passage interrompu", str(e)[:200])
    finally:
        STATE["running"] = False
        STATE["ended"] = db.now()
        text = recap(summary)
        STATE["summary"] = text
        db.x("UPDATE runs SET ended=?, summary=? WHERE id=?", (db.now(), text, run_id))
        phase("terminé", "")
        if summary["applied"] or summary["contacts"] or summary["followups"] or summary["missing"] or \
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
        await apply_all(browser, redo, summary)


def recap(s) -> str:
    L = []
    if s["applied"]:
        L.append("1) Candidatures envoyées :")
        L += [f"- {o['company']} — {o.get('poste') or o['title']} — {o['url']}" for o in s["applied"]]
    else:
        L.append("1) Aucune nouvelle candidature envoyée.")
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
        browser = await cdp.get_browser()
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
