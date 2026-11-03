"""Serveur local (127.0.0.1) : tableau de bord, API, pont WebSocket de l'extension Chrome, planification 11h/19h."""
import asyncio
import datetime as dt
import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import cdp, db, pipeline, providers, vault
from .config import CANDIDATURES, DATA, PKG, load_settings, save_settings
from .notify import ALERTS, LOG, log
from .sites import SITES

app = FastAPI(title="StageAuto")
_task = None


@app.on_event("startup")
async def _startup():
    db.import_seed()
    asyncio.create_task(scheduler())


async def scheduler():
    """Lance un passage aux heures prévues (11:00 et 19:00 par défaut) tant que l'application est ouverte."""
    done = set()
    while True:
        now = dt.datetime.now()
        for hm in load_settings().get("schedule", []):
            tag = f"{now.date()} {hm}"
            if now.strftime("%H:%M") == hm and tag not in done:
                done.add(tag)
                start_run(f"planifié {hm}")
        await asyncio.sleep(20)


def start_run(trigger="manuel"):
    global _task
    if pipeline.STATE["running"]:
        return False
    _task = asyncio.create_task(pipeline.run_pass(trigger))
    return True


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
    applied_today = db.q("SELECT key, site, company, title, url, folder, applied_at FROM offers WHERE status='applied' AND applied_at LIKE ? ORDER BY applied_at DESC", (today + "%",))
    st = dict(pipeline.STATE)
    st.pop("stop", None)
    return {
        "state": st, "extension": bool(cdp.BRIDGE.ws), "backend": s.get("browser_backend"),
        "api_key": bool(vault.get_api_key()), "foreign_key": bool(vault.get_foreign_key()),
        "provider": s.get("llm_provider", "anthropic"), "counts": counts, "applied_today": applied_today,
        "invites_today": db.invites_today(), "inmails_today": db.inmails_today(),
        "creds": vault.sites_with_creds(list(SITES) + ["gmail"]),
        "alerts": list(ALERTS)[-8:], "log": [l["t"] for l in list(LOG)[-120:]],
        "sites": {k: v.name for k, v in SITES.items()},
        "total_applied": db.q("SELECT COUNT(*) n FROM offers WHERE status='applied'")[0]["n"],
    }


@app.post("/api/run")
async def run():
    return {"started": start_run("manuel")}


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
    sql = "SELECT key, site, company, title, url, status, reason, folder, found_at, applied_at FROM offers WHERE found_at >= ?"
    args = [since]
    if status:
        sql += " AND status=?"
        args.append(status)
    return db.q(sql + " ORDER BY COALESCE(applied_at, found_at) DESC LIMIT 400", args)


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


@app.post("/api/apikey")
async def set_key(req: Request):
    vault.set_api_key((await req.json()).get("key", "").strip())
    providers.reset()
    return {"ok": bool(vault.get_api_key())}


@app.post("/api/foreign_key")
async def set_foreign_key(req: Request):
    vault.set_foreign_key((await req.json()).get("key", "").strip())
    return {"ok": bool(vault.get_foreign_key())}


@app.get("/api/learning")
async def learning():
    recipes = {r["site"]: json.loads(r["data"]).get("recipe") for r in db.q("SELECT * FROM methods WHERE site LIKE '%|%'")}
    answers = db.q("SELECT question, answer, source, uses FROM answers ORDER BY uses DESC, rowid DESC LIMIT 200")
    return {"curve": db.learning_curve(), "recipes": recipes, "answers": answers}


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


@app.post("/api/check_connections")
async def check_conn():
    b = await cdp.get_browser()
    ids = [k for k, v in load_settings().get("sites_enabled", {}).items() if v and k in SITES]
    pipeline.STATE["connections"] = await pipeline.check_connections(b, ids)
    return pipeline.STATE["connections"]


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
