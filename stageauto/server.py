"""Serveur local (127.0.0.1) : tableau de bord, API, pont WebSocket de l'extension Chrome, planification 11h/19h."""
import asyncio
import datetime as dt
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import CODE, cdp, db, pipeline, providers, sources, vault
from .config import CANDIDATURES, DATA, IS_WIN, PKG, load_settings, save_settings
from .notify import ALERTS, LOG, log
from .sites import SITES

app = FastAPI(title="StageAuto")


@app.exception_handler(cdp.NoBrowser)
async def _no_browser(request, exc):
    return JSONResponse({"ok": False, "error": str(exc)})
_task = None


@app.on_event("startup")
async def _startup():
    providers.migrate_settings()
    db.import_seed()
    sources.setup()
    fixed = db.repair()
    if fixed:
        log(f"{fixed} offre(s) corrigée(s) : « déjà postulé » n'est plus compté comme envoyé aujourd'hui, entreprise relue")
    n = db.requeue_ai_failures()
    if n:
        log(f"{n} offre(s) écartée(s) faute d'IA remises en attente")
    asyncio.create_task(scheduler())


def _in_hours(spec: str, now: dt.datetime) -> bool:
    try:
        a, b = [x.strip() for x in (spec or "00:00-23:59").split("-")]
        return a <= now.strftime("%H:%M") <= b
    except ValueError:
        return True


async def scheduler():
    """Passages complets aux heures prévues (11:00, 19:00) et veille rapide entre les deux, tant que l'application tourne."""
    done = set()
    next_quick = dt.datetime.now() + dt.timedelta(minutes=2)
    pending_full = pending_sweep = False
    while True:
        s = load_settings()
        now = dt.datetime.now()
        for hm in s.get("schedule", []):
            tag = f"{now.date()} {hm}"
            if now.strftime("%H:%M") == hm and tag not in done:
                done.add(tag)
                pending_full = True
        sweep = (s.get("sweep_time") or "").strip()
        if sweep and now.strftime("%H:%M") == sweep and f"{now.date()} sweep" not in done:
            done.add(f"{now.date()} sweep")
            pending_sweep = True
        if pending_sweep and not pipeline.STATE["running"]:
            # rattrapage du soir : toutes les sources sur 26 h, pour qu'aucune offre du jour ne passe entre deux passages
            pending_sweep = False
            start_run(f"rattrapage du soir {now:%H:%M}", "sweep")
        if RETRY and not pipeline.STATE["running"]:
            global _task
            _task = asyncio.create_task(pipeline.run_retry(list(RETRY)))
            RETRY.clear()
        resume_at = pipeline.STATE.get("ai_resume_at")
        if resume_at and not pipeline.STATE["running"] and now >= dt.datetime.fromisoformat(resume_at):
            # limite d'IA réinitialisée : passage complet tout de suite (offres gardées en attente d'abord)
            pipeline.STATE["ai_resume_at"] = None
            start_run("reprise après la limite d'IA", "full")
            next_quick = now + dt.timedelta(minutes=s.get("watch_interval_min", 30))
        if not pipeline.STATE["running"]:
            last_full = pipeline.STATE.get("last_full_started")
            if pending_full and last_full and (now - last_full).total_seconds() < 1800:
                pending_full = False  # déjà lancé par la tâche planifiée Windows : pas de doublon
            if pending_full:
                pending_full = False
                start_run(f"planifié {now:%H:%M}", "full")
                next_quick = now + dt.timedelta(minutes=s.get("watch_interval_min", 30))
            elif s.get("watch_enabled", True) and _in_hours(s.get("watch_hours"), now) and now >= next_quick:
                start_run("veille", "quick")
                jitter = random.uniform(0.85, 1.15)
                next_quick = now + dt.timedelta(minutes=s.get("watch_interval_min", 30) * jitter)
        pipeline.STATE["next_check"] = next_quick.isoformat(timespec="minutes") if s.get("watch_enabled", True) else None
        await asyncio.sleep(15)


def start_run(trigger="manuel", mode="full"):
    global _task
    if pipeline.STATE["running"]:
        return False
    _task = asyncio.create_task(pipeline.run_pass(trigger, mode))
    return True


@app.get("/favicon.ico")
async def favicon():
    from fastapi.responses import Response
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="#1f7a4d"/>'
           '<path d="M5 8.5l2 2 4-4.5" stroke="#fff" stroke-width="1.8" fill="none"/></svg>')
    return Response(svg, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (PKG / "static" / "index.html").read_text(encoding="utf-8")


@app.websocket("/ext")
async def ext(ws: WebSocket):
    await ws.accept()
    await cdp.BRIDGE.attach(ws)
    try:
        while True:
            cdp.BRIDGE.on_message(json.loads(await ws.receive_text()))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if cdp.BRIDGE.ws is ws:
            cdp.BRIDGE.detach()


@app.get("/api/state")
async def state():
    s = load_settings()
    today = db.today()
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM offers WHERE found_at LIKE ? GROUP BY status", (today + "%",))}
    applied_today = db.q("SELECT key, site, company, title, url, folder, applied_at, found_at, data FROM offers WHERE status='applied' "
                         "AND applied_at LIKE ? ORDER BY applied_at DESC", (today + "%",))
    for r in applied_today:
        d = json.loads(r.pop("data") or "{}")
        r["verified"] = d.get("verified", "")
        try:
            r["delay_min"] = round((dt.datetime.fromisoformat(r["applied_at"]) - dt.datetime.fromisoformat(r["found_at"])).total_seconds() / 60)
        except (TypeError, ValueError):
            r["delay_min"] = None
    st = dict(pipeline.STATE)
    st.pop("stop", None)
    st.pop("connections_ts", None)
    st.pop("linkedin_last_search", None)
    st.pop("last_full_started", None)
    return {
        "code": CODE, "state": st, "extension": bool(cdp.BRIDGE.ws), "backend": s.get("browser_backend"),
        "watch": {"enabled": s.get("watch_enabled", True), "interval": s.get("watch_interval_min", 30),
                  "hours": s.get("watch_hours")},
        "ai": {"ready": providers.ready(wait=False), "order": [providers.label(p) for p in providers.providers_in_order(wait=False)],
               "primary": providers.label(s.get("llm_provider", "anthropic")),
               "why": providers.configured(s.get("llm_provider", "anthropic"), wait=False)[1]},
        "counts": counts, "applied_today": applied_today,
        "invites_today": db.invites_today(), "inmails_today": db.inmails_today(),
        "creds": vault.sites_with_creds(list(SITES) + ["gmail"]),
        "alerts": list(ALERTS)[-8:], "log": [l["t"] for l in list(LOG)[-120:]],
        "sites": {k: v.name for k, v in SITES.items()},
        "sources": {"active": db.q("SELECT COUNT(*) n FROM sources WHERE status='active'")[0]["n"],
                    "found_today": sum(r["new"] for r in db.coverage_today()
                                       if r["source"] not in SITES and r["source"] != "alertes e-mail")},
        "total_applied": db.q("SELECT COUNT(*) n FROM offers WHERE status='applied'")[0]["n"],
        "failed_today": len(pipeline.failed_offers(1)), "failed_list": pipeline.failed_offers(1)[:15], "retry_waiting": len(RETRY), "hot_replies": hot_replies(),
    }


@app.post("/api/run")
async def run(mode: str = "full"):
    return {"started": start_run("manuel", "quick" if mode == "quick" else "full")}


RETRY: set = set()  # offres à réessayer pendant qu'un passage est en cours : lancées dès qu'il se termine


@app.post("/api/retry")
async def retry(req: Request):
    """Bouton « Réessayer » : une offre (key) ou tous les échecs des offres du jour (all)."""
    b = await req.json()
    keys = [b["key"]] if b.get("key") else [f["key"] for f in pipeline.failed_offers(int(b.get("days") or 1))]
    keys = pipeline.requeue(keys)
    if not keys:
        return {"ok": True, "count": 0, "started": False}
    RETRY.update(keys)
    started = False
    if not pipeline.STATE["running"]:
        global _task
        _task = asyncio.create_task(pipeline.run_retry(list(RETRY)))
        RETRY.clear()
        started = True
    return {"ok": True, "count": len(keys), "started": started}


@app.get("/api/failed")
async def failed(days: int = 1):
    return pipeline.failed_offers(days)


@app.post("/api/test_linkedin")
async def test_linkedin():
    if pipeline.STATE["running"]:
        return {"started": False}
    asyncio.create_task(pipeline.test_linkedin())
    return {"started": True}


@app.post("/api/stop")
async def stop():
    pipeline.STATE["stop"] = True
    return {"ok": True}


@app.get("/api/offers")
async def offers(status: str = "", days: int = 7):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    sql = "SELECT key, site, company, title, url, status, reason, folder, found_at, applied_at, data FROM offers WHERE found_at >= ?"
    args = [since]
    if status and status not in ("failed", "ecartees"):
        sql += " AND status=?"
        args.append(status)
    elif status == "ecartees":
        sql += " AND status='ignored'"
    rows = db.q(sql + " ORDER BY COALESCE(applied_at, found_at) DESC LIMIT 400", args)
    for r in rows:
        d = json.loads(r.pop("data") or "{}")
        r["verified"], r["score"], r["reponse"] = d.get("verified", ""), d.get("score"), d.get("reponse", "")
        r["retryable"] = pipeline.is_failed(r["status"], r["reason"], d)
    if status == "failed":
        return [r for r in rows if r["retryable"]]
    if status == "ecartees":
        return [r for r in rows if not r["retryable"]]
    if not status:  # liste par défaut : sans les offres non compatibles (non éligibles, pré-tri, hors domaine, anciennes)
        return [r for r in rows if r["status"] != "ignored" or r["retryable"]]
    return rows


@app.get("/api/contacts")
async def contacts():
    return db.q("SELECT date, company, offer, name, role, profile_url, status FROM contacts WHERE kind != 'seed' ORDER BY id DESC LIMIT 300")


@app.get("/api/settings")
async def get_settings():
    return load_settings()


@app.post("/api/settings")
async def set_settings(req: Request):
    body = await req.json()
    s = load_settings()
    s.update({k: v for k, v in body.items() if k in s})
    save_settings(s)
    if "browser_backend" in body:
        await cdp.reset_browser()
    return s


@app.get("/api/profile")
async def get_profile():
    """Ce que l'application sait de toi : profil des formulaires (profile.json) et profil du skill (profil.md)."""
    from .config import PROFILE, SKILL_DIR
    md = SKILL_DIR / "references" / "profil.md"
    return {"profile": PROFILE, "profil_md": md.read_text(encoding="utf-8") if md.exists() else "",
            "files": {"profile": str(_profile_path()), "profil_md": str(md)}}


def _profile_path():
    from .config import PERSONAL
    return PERSONAL / "profile.json"


@app.post("/api/profile")
async def set_profile(req: Request):
    """Enregistre le profil (copie de l'ancien en .bak) ; pris en compte tout de suite, sans redémarrer."""
    from . import cvskill
    from .config import PROFILE, SKILL_DIR
    b = await req.json()
    if isinstance(b.get("profile"), dict):
        pf = _profile_path()
        pf.parent.mkdir(parents=True, exist_ok=True)
        if pf.exists():
            pf.with_suffix(".json.bak").write_text(pf.read_text(encoding="utf-8"), encoding="utf-8")
        pf.write_text(json.dumps(b["profile"], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        PROFILE.clear()
        PROFILE.update(b["profile"])
    if isinstance(b.get("profil_md"), str) and b["profil_md"].strip():
        md = SKILL_DIR / "references" / "profil.md"
        if md.exists():
            md.with_suffix(".md.bak").write_text(md.read_text(encoding="utf-8"), encoding="utf-8")
        md.write_text(b["profil_md"], encoding="utf-8")
        cvskill._SYS = None  # consignes du skill relues au prochain CV
    log("profil mis à jour depuis le tableau de bord")
    return {"ok": True}


def _paths() -> dict:
    from .config import LOG_PATH, PERSONAL
    return {"cv_prepares": {"label": "CV et lettres préparés (un dossier par offre, PDF dans « out »)", "path": str(DATA / "work")},
            "cv_envoyes": {"label": "CV et lettres envoyés (copie par candidature)", "path": str(CANDIDATURES / "envoyees")},
            "candidatures": {"label": "Dossier Candidatures (certificats, envoyées)", "path": str(CANDIDATURES)},
            "personnel": {"label": "Ton profil et ton skill (profile.json, skill/)", "path": str(PERSONAL)},
            "donnees": {"label": "Données de l'application (base, réglages, journaux)", "path": str(DATA)},
            "journal": {"label": "Journal détaillé", "path": str(LOG_PATH)}}


@app.get("/api/paths")
async def paths():
    out = _paths()
    for v in out.values():
        p = Path(v["path"])
        v["exists"] = p.exists()
        v["pdf"] = len(list(p.rglob("*.pdf"))) if p.is_dir() else None
    out["recent"] = [{"company": r["company"], "title": r["title"], "folder": r["folder"], "status": r["status"]}
                     for r in db.q("SELECT company, title, folder, status FROM offers WHERE folder IS NOT NULL AND folder != '' "
                                   "ORDER BY COALESCE(applied_at, found_at) DESC LIMIT 15")]
    return out


@app.post("/api/open_folder")
async def open_folder(req: Request):
    """Ouvre le dossier dans l'Explorateur Windows."""
    b = await req.json()
    p = Path(_paths().get(b.get("which", ""), {}).get("path") or b.get("path") or "")
    allowed = [Path(v["path"]).resolve() for v in _paths().values()]
    if not p or not any(str(p.resolve()).startswith(str(a)) for a in allowed):
        return {"ok": False, "error": "dossier non autorisé"}
    target = p if p.is_dir() else p.parent
    target.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        os.startfile(str(target))  # noqa
        return {"ok": True}
    return {"ok": False, "path": str(target), "error": "ouverture possible seulement sous Windows"}


@app.post("/api/apikey")
async def set_key(req: Request):
    vault.set_api_key((await req.json()).get("key", "").strip())
    providers.reset()
    return {"ok": bool(vault.get_api_key())}


@app.get("/api/ai")
async def ai_info():
    return providers.describe(wait=False)


@app.post("/api/provider_key")
async def set_provider_key(req: Request):
    b = await req.json()
    pid = b.get("provider", "")
    if pid not in providers.PROVIDERS:
        return {"ok": False}
    vault.set_provider_key(pid, (b.get("key") or "").strip())
    providers.reset()
    return {"ok": True, "configured": providers.configured(pid, wait=False)}


@app.post("/api/claude_code_diag")
async def claude_code_diag():
    return await asyncio.to_thread(providers.claude_diag)


@app.post("/api/test_ai")
async def test_ai(req: Request):
    b = await req.json()
    pid = b.get("provider") or load_settings().get("llm_provider", "anthropic")
    return await asyncio.to_thread(providers.test, pid)


@app.get("/api/sources")
async def list_sources():
    """Sites carrières et autres sites d'emploi surveillés, avec leur état."""
    rows = sources.all_sources()
    for r in rows:
        r["platform_label"] = sources.PLATFORMS.get(r["platform"], sources.Platform(r["platform"], r["platform"], False, None)).label
        r.pop("params", None)
    sr = load_settings().get("smartrecruiters_companies") or []
    return {"sources": rows, "smartrecruiters": sr}


@app.post("/api/sources")
async def add_source(req: Request):
    b = await req.json()
    return sources.add_manual(b.get("url", ""), b.get("company", ""))


@app.post("/api/sources/test")
async def test_source(req: Request):
    b = await req.json()
    browser = None
    if cdp.BRIDGE.ws:
        try:
            browser = await cdp.get_browser(wait_ext=False)
        except Exception:
            browser = None
    return await sources.test(b.get("key", ""), browser)


@app.post("/api/sources/toggle")
async def toggle_source(req: Request):
    b = await req.json()
    r = db.q("SELECT status FROM sources WHERE key=?", (b.get("key", ""),))
    if not r:
        return {"ok": False}
    new = "désactivée" if r[0]["status"] == "active" else "active"
    db.x("UPDATE sources SET status=?, fails=0, account_blocks=0 WHERE key=?", (new, b["key"]))
    return {"ok": True, "status": new}


@app.post("/api/sources/delete")
async def delete_source(req: Request):
    b = await req.json()
    db.x("DELETE FROM sources WHERE key=?", (b.get("key", ""),))
    db.x("DELETE FROM source_seen WHERE source=?", (b.get("key", ""),))
    return {"ok": True}


@app.get("/api/coverage")
async def coverage():
    """Aujourd'hui : offres listées et nouvelles par source (sites, sites carrières, alertes e-mail)."""
    out = []
    for r in db.coverage_today():
        r["name"] = SITES[r["source"]].name if r["source"] in SITES else r["source"]
        out.append(r)
    return out


@app.get("/api/replies")
async def replies():
    """Réponses des recruteurs repérées dans Gmail (entretien, refus, autre), les plus récentes d'abord."""
    out = []
    for r in db.q("SELECT site, key, note, created FROM pending WHERE kind='reply' ORDER BY id DESC LIMIT 40"):
        d = json.loads(r["note"] or "{}")
        out.append({"kind": r["site"], "key": r["key"], "date": r["created"], "company": d.get("company") or d.get("who", ""),
                    "title": d.get("title", ""), "subject": d.get("subject", ""), "account": d.get("account", ""),
                    "link": d.get("link", ""), "excerpt": d.get("excerpt", "")})
    return out


def hot_replies() -> list:
    """Offres et propositions d'échange des 7 derniers jours : mises en avant dans la carte « Aujourd'hui »."""
    since = (dt.date.today() - dt.timedelta(days=6)).isoformat()
    out = []
    for r in db.q("SELECT site, note, created FROM pending WHERE kind='reply' AND site IN ('offre','entretien') AND created >= ? "
                  "ORDER BY id DESC LIMIT 10", (since,)):
        d = json.loads(r["note"] or "{}")
        out.append({"kind": r["site"], "date": r["created"], "company": d.get("company") or d.get("who", ""),
                    "subject": d.get("subject", ""), "link": d.get("link", ""), "account": d.get("account", "")})
    return out


@app.get("/api/export.csv")
async def export_csv():
    """Toutes les candidatures (pour un tableur) : séparateur « ; » et UTF-8 avec BOM, lisibles directement par Excel."""
    import csv
    import io
    from fastapi.responses import Response
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Date", "Entreprise", "Offre", "Site", "Statut", "Vérification", "Réponse", "Lien", "Dossier"])
    for r in db.q("SELECT key, site, company, title, url, status, folder, applied_at, found_at, data FROM offers "
                  "WHERE status IN ('applied','already','ready','docs_ready','blocked_login') ORDER BY COALESCE(applied_at, found_at) DESC"):
        d = json.loads(r["data"] or "{}")
        w.writerow([(r["applied_at"] or r["found_at"] or "")[:16].replace("T", " "), r["company"], r["title"],
                    SITES[r["site"]].name if r["site"] in SITES else r["site"], r["status"], d.get("verified", ""),
                    d.get("reponse", ""), r["url"], r["folder"] or ""])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="candidatures_{db.today()}.csv"'})


@app.get("/api/learning")
async def learning():
    recipes = {r["site"]: json.loads(r["data"]).get("recipe") for r in db.q("SELECT * FROM methods WHERE site LIKE '%|%'")}
    answers = db.q("SELECT question, answer, source, uses FROM answers ORDER BY uses DESC, rowid DESC LIMIT 200")
    return {"curve": db.learning_curve(), "recipes": recipes, "answers": answers, "delays": db.delay_stats()}


@app.post("/api/cred")
async def set_cred(req: Request):
    b = await req.json()
    vault.set_cred(b["site"], b.get("user", ""), b.get("password", ""))
    return {"ok": True}


@app.post("/api/import_proton")
async def import_proton(file: UploadFile = File(...)):
    tmp = Path(tempfile.mkstemp(suffix=".csv", dir=DATA)[1])
    tmp.write_bytes(await file.read())
    try:
        r = vault.import_proton_csv(str(tmp), delete_after=True)
    finally:
        tmp.unlink(missing_ok=True)
    log(f"Proton Pass : {len(r['imported'])} identifiants importés ({', '.join(r['imported'])})")
    return r


@app.post("/api/open_login/{site}")
async def open_login(site: str):
    """Ouvre la page de connexion du site dans la fenêtre StageAuto de Chrome (tu te connectes toi-même)."""
    b = await cdp.get_browser(wait_ext=3)
    if site == "gmail":
        url = "https://mail.google.com/"
    else:
        sd = SITES[site]
        url = sd.login_url or sd.home
    await b.new_tab(url)
    if b.kind == "extension":
        try:
            await cdp.BRIDGE.call("window.show")
        except Exception:
            pass
    return {"ok": True}


async def _exit_soon():
    await asyncio.sleep(0.4)
    try:
        await cdp.reset_browser()
    except Exception:
        pass
    os._exit(0)


@app.post("/api/quit")
async def quit_app():
    """Arrêt de l'application (utilisé par StageAuto.bat quand une nouvelle version est installée)."""
    asyncio.create_task(_exit_soon())
    return {"ok": True}


@app.post("/api/restart")
async def restart_app():
    """Bouton « Redémarrer » : relance l'application avec le code actuel des fichiers."""
    flags = (0x00000008 | 0x00000200) if IS_WIN else 0  # processus détaché sous Windows
    subprocess.Popen([sys.executable, "-m", "stageauto", "--restart", "--no-open"], cwd=str(PKG.parent),
                     creationflags=flags, close_fds=True)
    asyncio.create_task(_exit_soon())
    return {"ok": True}


@app.post("/api/show_browser")
async def show_browser():
    """« Voir Chrome » : met les fenêtres StageAuto de ton Chrome au premier plan."""
    if cdp.BRIDGE.ws:
        try:
            n = await cdp.BRIDGE.call("window.show", lane="all", timeout=15)
            return {"ok": True, "windows": n}
        except Exception as e:
            return {"ok": False, "error": f"Chrome n'a pas pu afficher les fenêtres ({e})"}
    return {"ok": False, "error": "L'extension StageAuto Bridge n'est pas connectée : clique sur son icône dans Chrome puis « Reconnecter »."}


@app.post("/api/check_connections")
async def check_conn():
    s = load_settings()
    if s.get("browser_backend", "extension") == "extension" and not cdp.BRIDGE.ws:
        # sans l'extension, la vérification se ferait dans le Chrome dédié, où tu n'es connecté nulle part
        return {"error": "L'extension StageAuto Bridge n'est pas connectée. Clique sur son icône dans Chrome puis "
                         "« Reconnecter ». Elle doit être installée dans le profil Chrome où tu es connecté à LinkedIn."}
    b = await cdp.get_browser(wait_ext=3)
    ids = [k for k, v in s.get("sites_enabled", {}).items() if v and k in SITES]
    res = await pipeline.check_connections(b, ids)
    pipeline.STATE["connections"].update(res)
    pipeline.STATE["connections_ts"] = dt.datetime.now()
    return {"connections": res, "info": pipeline.STATE.get("connections_info", {}), "browser": b.kind}


@app.post("/api/review/{key}")
async def review(key: str, action: str = "submit"):
    if action == "discard":
        r = pipeline.STATE["review"].pop(key, None)
        db.set_status(key, "ignored", "abandonnée à la validation")
        return {"ok": bool(r)}
    return {"status": await pipeline.submit_review(key)}


@app.get("/files")
async def files(path: str):
    p = Path(path).resolve()
    allowed = [CANDIDATURES.resolve(), (DATA / "work").resolve()]
    if not any(str(p).startswith(str(a)) for a in allowed) or not p.exists():
        return JSONResponse({"error": "interdit"}, status_code=403)
    if p.is_dir():
        from urllib.parse import quote
        links = "".join(f'<li><a href="/files?path={quote(str(f))}">{f.name}</a></li>' for f in sorted(p.glob("*.pdf")))
        return HTMLResponse(f"<meta charset=utf-8><body style='font:14px system-ui;margin:20px'><h3>{p.name}</h3>"
                            f"<p>{p}</p><ul>{links}</ul></body>")
    return FileResponse(p)


@app.get("/api/notes")
async def notes():
    return {r["site"]: json.loads(r["data"]) for r in db.q("SELECT * FROM methods WHERE site NOT LIKE '\\_%' ESCAPE '\\'")
            if "|" not in r["site"]}
