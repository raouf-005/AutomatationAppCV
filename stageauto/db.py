"""Base SQLite : offres vues, candidatures, contacts LinkedIn, réponses apprises, méthodes par site."""
import datetime as dt
import functools
import json
import re
import sqlite3
import threading
import unicodedata

from .config import DB_PATH, SEED_DIR

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers(
  key TEXT PRIMARY KEY, site TEXT, company TEXT, title TEXT, url TEXT, location TEXT,
  posted TEXT, found_at TEXT, status TEXT, reason TEXT, folder TEXT, apply_url TEXT,
  norm TEXT, lang TEXT, data TEXT, applied_at TEXT, duration REAL);
CREATE INDEX IF NOT EXISTS offers_norm ON offers(norm);
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, company TEXT, offer TEXT, name TEXT, role TEXT,
  profile_url TEXT UNIQUE, status TEXT, folder TEXT, kind TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS answers(qkey TEXT PRIMARY KEY, question TEXT, answer TEXT, source TEXT, uses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS methods(site TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS hosts(host TEXT PRIMARY KEY, verdict TEXT, note TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, ended TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS apply_stats(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, site TEXT, rkey TEXT, mode TEXT,
  seconds REAL, llm_steps INTEGER, status TEXT, provider TEXT);
CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, site TEXT, key TEXT, note TEXT, created TEXT);
"""
with _lock:
    _conn.executescript(SCHEMA)
    _conn.commit()


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return dt.date.today().isoformat()


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def x(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b[fh]\s*/\s*[hf]\b|\(.*?\)|stage|intern(ship)?|-|–", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def offer_norm(company: str, title: str) -> str:
    return norm_text(company)[:30] + "|" + norm_text(title)[:80]


# ---------- même offre sur plusieurs sites ----------
STOP = set("de des du la le les l d un une et en au aux a pour par sur dans avec of the and for in on to at h f fh hf x "
           "ou o e euse eur trice".split())
GENERIC_CO = set("groupe group sa sas sasu se spa inc ltd gmbh france the le la les".split())


@functools.lru_cache(maxsize=8192)
def title_tokens(title: str) -> frozenset:
    """Mots distinctifs d'un intitulé, accents et pluriels retirés (réseaux = réseau, optiques = optique)."""
    toks = [t for t in norm_text(title).split() if len(t) > 1 and t not in STOP]
    return frozenset(t[:-1] if len(t) > 4 and t[-1] in "sx" else t for t in toks)


@functools.lru_cache(maxsize=4096)
def company_key(company: str) -> str:
    """« Orange », « ORANGE SA », « Groupe Orange » -> « orange »."""
    toks = [t for t in norm_text(company).split() if t not in GENERIC_CO]
    return toks[0] if toks else ""


def same_offer(c1: str, t1: str, c2: str, t2: str) -> bool:
    """Même offre publiée sur deux sites (HelloWork / orange.jobs…) : même entreprise et intitulés presque identiques,
    ou l'un contenu dans l'autre (l'historique note des intitulés abrégés : « Orange Résilience réseaux agents IA »)."""
    k1, k2 = company_key(c1 or ""), company_key(c2 or "")
    drop = {k1, k2} - {""}
    A, B = title_tokens(t1 or "") - drop, title_tokens(t2 or "") - drop
    if not A or not B:
        return False
    inter = len(A & B)
    close = inter / len(A | B) >= 0.8 or (min(len(A), len(B)) >= 3 and inter / min(len(A), len(B)) >= 0.9)
    if k1 and k2:
        return k1 == k2 and close
    # entreprise inconnue d'un côté : seulement un intitulé long et presque identique
    return len(A) >= 6 and len(B) >= 6 and inter / len(A | B) >= 0.9


DONE = ("applied", "already", "ready", "blocked_login", "docs_ready")


def duplicate_of(company: str, title: str, exclude_key: str = "", statuses=DONE):
    """Offre déjà traitée (postulée, CV prêt…) qui est la même que celle-ci, sous une autre clé ; sinon None."""
    if not title:
        return None
    for r in q(f"SELECT key, company, title, status FROM offers WHERE status IN ({','.join('?' * len(statuses))})", statuses):
        if r["key"] != exclude_key and same_offer(company, title, r["company"] or "", r["title"] or ""):
            return r
    return None


# ---------- offres ----------
def offer_seen(key: str, company: str = "", title: str = "") -> bool:
    if q("SELECT 1 FROM offers WHERE key=?", (key,)):
        return True
    return bool(duplicate_of(company, title, key))


def upsert_offer(o: dict) -> None:
    o = dict(o)
    o.setdefault("found_at", now())
    o["norm"] = offer_norm(o.get("company", ""), o.get("title", ""))
    data = {k: v for k, v in o.items() if k not in COLS}
    row = {k: o.get(k) for k in COLS if k != "data"}
    old = q("SELECT data FROM offers WHERE key=?", (o["key"],))
    if old and old[0]["data"]:
        merged = json.loads(old[0]["data"])
        merged.update(data)
        data = merged
    row["data"] = json.dumps(data, ensure_ascii=False)
    cols = ",".join(row)
    upd = ",".join(f"{k}=COALESCE(excluded.{k},{k})" for k in row if k != "key")
    x(f"INSERT INTO offers({cols}) VALUES({','.join('?' * len(row))}) ON CONFLICT(key) DO UPDATE SET {upd}",
      tuple(row.values()))


COLS = ["key", "site", "company", "title", "url", "location", "posted", "found_at", "status", "reason",
        "folder", "apply_url", "norm", "lang", "data", "applied_at", "duration"]


def set_status(key: str, status: str, reason: str = "", **kw) -> None:
    sets = ["status=?", "reason=?"]
    args = [status, reason[:500]]
    for k, v in kw.items():
        sets.append(f"{k}=?")
        args.append(v)
    if status == "applied":
        sets.append("applied_at=?")
        args.append(now())
    x(f"UPDATE offers SET {','.join(sets)} WHERE key=?", (*args, key))


def applied_today_for(company: str) -> int:
    """Candidatures envoyées aujourd'hui à cette entreprise, quelle que soit l'écriture du nom (Orange, ORANGE SA…)."""
    k = company_key(company or "")
    rows = q("SELECT company FROM offers WHERE status='applied' AND applied_at LIKE ?", (today() + "%",))
    return sum(1 for r in rows if (company_key(r["company"] or "") == k if k else (r["company"] or "") == (company or "")))


def repair() -> int:
    """Corrige ce qu'ont enregistré les versions précédentes : offre « déjà postulée » comptée comme envoyée aujourd'hui,
    entreprise lue dans le bouton « Ajouter l'offre … aux favoris » de HelloWork."""
    n = x("UPDATE offers SET status='already', applied_at=NULL WHERE status='applied' AND reason LIKE 'déjà postulé%'").rowcount
    for r in q("SELECT key, company, title, data FROM offers WHERE company LIKE '%favoris%'"):
        d = json.loads(r["data"] or "{}")
        comp = d.get("company_ai") or (d.get("dossier") or "").split("_")[0]
        x("UPDATE offers SET company=?, norm=? WHERE key=?", (comp, offer_norm(comp, r["title"] or ""), r["key"]))
        n += 1
    return n


# ---------- contacts ----------
def contact_known(name: str = "", url: str = "") -> bool:
    if url and q("SELECT 1 FROM contacts WHERE profile_url=?", (url.split("?")[0].rstrip("/"),)):
        return True
    if name:
        n = norm_text(name)
        return any(norm_text(r["name"]) == n for r in q("SELECT name FROM contacts"))
    return False


def add_contact(company, offer, name, role, url, status, folder, kind) -> None:
    x("INSERT OR IGNORE INTO contacts(date,company,offer,name,role,profile_url,status,folder,kind,updated) "
      "VALUES(?,?,?,?,?,?,?,?,?,?)",
      (today(), company, offer, name, role, (url or "").split("?")[0].rstrip("/") or None, status, folder, kind, now()))


def invites_today() -> int:
    return q("SELECT COUNT(*) n FROM contacts WHERE status IN ('INVITE') AND date=?", (today(),))[0]["n"]


def inmails_today() -> int:
    return q("SELECT COUNT(*) n FROM contacts WHERE status IN ('INMAIL') AND date=?", (today(),))[0]["n"]


# ---------- réponses apprises ----------
def qkey(question: str, options=None) -> str:
    k = norm_text(question)[:200]
    if options:
        k += "||" + "|".join(sorted(norm_text(o)[:40] for o in options))[:300]
    return k


def get_answer(question, options=None):
    r = q("SELECT answer FROM answers WHERE qkey=?", (qkey(question, options),))
    if r:
        x("UPDATE answers SET uses=uses+1 WHERE qkey=?", (qkey(question, options),))
        return r[0]["answer"]
    return None


def put_answer(question, options, answer, source="llm") -> None:
    x("INSERT OR REPLACE INTO answers(qkey,question,answer,source,uses) VALUES(?,?,?,?,0)",
      (qkey(question, options), question[:300], answer, source))


# ---------- méthodes par site (apprentissage) ----------
def method(site: str) -> dict:
    r = q("SELECT data FROM methods WHERE site=?", (site,))
    return json.loads(r[0]["data"]) if r else {}


def save_method(site: str, data: dict) -> None:
    x("INSERT OR REPLACE INTO methods(site,data) VALUES(?,?)", (site, json.dumps(data, ensure_ascii=False)))


def learn(site: str, **kv) -> None:
    """Mémorise ce qui a marché (texte de bouton, durée, étapes) pour aller plus vite au passage suivant."""
    m = method(site)
    for k, v in kv.items():
        if k == "duration":
            st = m.setdefault("stats", {"n": 0, "avg": 0})
            st["avg"] = round((st["avg"] * st["n"] + v) / (st["n"] + 1), 1)
            st["n"] += 1
        elif isinstance(v, str) and k.endswith("_btn"):
            lst = m.setdefault(k, [])
            if v in lst:
                lst.remove(v)
            lst.insert(0, v)
            del lst[6:]
        else:
            m[k] = v
    save_method(site, m)


def host_verdict(host: str):
    r = q("SELECT verdict FROM hosts WHERE host=?", (host,))
    return r[0]["verdict"] if r else None


def set_host(host: str, verdict: str, note: str = "") -> None:
    x("INSERT OR REPLACE INTO hosts(host,verdict,note,updated) VALUES(?,?,?,?)", (host, verdict, note, now()))


# ---------- import de l'historique (seen.txt, contacts.md) ----------
def import_seed() -> int:
    if q("SELECT 1 FROM methods WHERE site='_seed'"):
        return 0
    n = 0
    seen = SEED_DIR / "seen.txt"
    if seen.exists():
        for line in seen.read_text(encoding="utf-8").splitlines():
            m = re.match(r"(\S+:\S+)\s+([ali])\s+(.*)", line)
            if not m:
                continue
            key, st, lab = m.groups()
            status = {"a": "applied", "i": "ignored", "l": "ignored"}[st]
            words = lab.split()
            upsert_offer({"key": key, "site": key.split(":")[0], "company": words[0] if words else "",
                          "title": lab, "status": status, "reason": "historique seen.txt",
                          "found_at": "2026-10-01T00:00:00", "applied_at": "2026-10-01T00:00:00" if status == "applied" else None})
            n += 1
    contacts = SEED_DIR / "contacts.md"
    if contacts.exists():
        txt = contacts.read_text(encoding="utf-8")
        for line in txt.splitlines():
            if line.startswith("- ") or line.startswith("Fabien"):
                body = line.split(":", 1)[-1]
                for part in re.split(r"[,;]", body):
                    nm = re.sub(r"\(.*?\)", "", part).replace(" et ", ",").strip(" .")
                    for name in nm.split(","):
                        name = name.strip()
                        if 3 < len(name) < 40 and name[0].isupper() and " " in name:
                            add_contact("", "", name, "", None, "HISTORIQUE", "", "seed")
        for name in re.findall(r"\|\s*([A-ZÉ][\wéèëïç'-]+(?: [A-ZÉ][\wéèëïç' -]+)+) \(/in/([^)]+)\)", txt):
            add_contact("", "", name[0], "", "https://www.linkedin.com/in/" + name[1], "HISTORIQUE", "", "seed")
    save_method("_seed", {"done": now(), "n": n})
    return n


# ---------- courbe d'apprentissage ----------
def record_apply(site, rkey, mode, seconds, llm_steps, status, provider=""):
    x("INSERT INTO apply_stats(ts,site,rkey,mode,seconds,llm_steps,status,provider) VALUES(?,?,?,?,?,?,?,?)",
      (now(), site, rkey, mode, seconds, llm_steps, status, provider))


def learning_curve():
    """Par site : candidatures, réussite, temps moyen des 5 premières vs 5 dernières, part rejouée sans IA."""
    out = {}
    for r in q("SELECT * FROM apply_stats ORDER BY id"):
        d = out.setdefault(r["site"], {"runs": []})
        d["runs"].append(r)
    res = []
    for site, d in out.items():
        runs = d["runs"]
        ok = [r for r in runs if r["status"] in ("applied", "ready")]
        secs = [r["seconds"] or 0 for r in ok]
        avg = lambda a: round(sum(a) / len(a)) if a else None  # noqa: E731
        res.append({"site": site, "n": len(runs), "ok": len(ok),
                    "first5": avg(secs[:5]), "last5": avg(secs[-5:]),
                    "replay_share": round(100 * sum(1 for r in ok if r["mode"] == "replay") / len(ok)) if ok else 0,
                    "llm_steps_first5": avg([r["llm_steps"] or 0 for r in ok[:5]]),
                    "llm_steps_last5": avg([r["llm_steps"] or 0 for r in ok[-5:]]),
                    "last": [{"s": r["seconds"], "m": r["mode"]} for r in ok[-20:]]})
    return sorted(res, key=lambda r: -r["n"])


def set_data(key: str, **kv) -> None:
    """Ajoute des informations à une offre (ex. vérification) sans toucher à ses autres colonnes."""
    r = q("SELECT data FROM offers WHERE key=?", (key,))
    if not r:
        return
    d = json.loads(r[0]["data"] or "{}")
    d.update(kv)
    x("UPDATE offers SET data=? WHERE key=?", (json.dumps(d, ensure_ascii=False), key))


def offer_data(key: str) -> dict:
    r = q("SELECT * FROM offers WHERE key=?", (key,))
    return {**r[0], **json.loads(r[0]["data"] or "{}")} if r else {}


def delay_stats(days=14):
    """Rapidité : minutes entre la détection d'une offre et la candidature, âge de l'offre au moment de postuler."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = {}
    for r in q("SELECT site, found_at, applied_at, data FROM offers WHERE status='applied' AND applied_at >= ? "
               "AND found_at >= ?", (since, since)):
        try:
            d = (dt.datetime.fromisoformat(r["applied_at"]) - dt.datetime.fromisoformat(r["found_at"])).total_seconds() / 60
        except (TypeError, ValueError):
            continue
        age = (json.loads(r["data"] or "{}").get("age_min"))
        s = out.setdefault(r["site"], {"n": 0, "delay": [], "age": [], "verified": 0})
        s["n"] += 1
        s["delay"].append(max(0, d))
        if age is not None:
            s["age"].append(age + d)
        if json.loads(r["data"] or "{}").get("verified"):
            s["verified"] += 1
    res = []
    for site, s in out.items():
        avg = lambda a: round(sum(a) / len(a)) if a else None  # noqa: E731
        res.append({"site": site, "n": s["n"], "delay_min": avg(s["delay"]), "age_at_apply_min": avg(s["age"]),
                    "verified": s["verified"]})
    return sorted(res, key=lambda r: -r["n"])


def requeue_ai_failures() -> int:
    """Offres écartées à tort parce que l'IA ne répondait pas (clé absente…) : remises en attente."""
    cur = x("UPDATE offers SET status='pending_ai', reason='en attente de l''IA (clé API)' WHERE status IN ('ignored','error') "
            "AND (reason LIKE '%skill indisponible%' OR reason LIKE '%adaptation impossible%' OR reason LIKE '%authentication%' "
            "OR reason LIKE '%IA indisponible%' OR reason LIKE '%TypeError%' OR reason LIKE '%clé d''IA%')")
    return cur.rowcount


def pending_ai_offers(days=3):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return [{**r, **json.loads(r["data"] or "{}")} for r in q("SELECT * FROM offers WHERE status='pending_ai' AND found_at >= ?", (since,))]
