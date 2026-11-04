"""Fournisseurs d'IA au choix, même interface pour toute l'application :

- Claude (clé API Anthropic) ;
- Claude Code : la commande « claude » installée sur ton PC, avec ton abonnement Claude (pas de clé API) ;
- ChatGPT (OpenAI), Gemini (Google), Kimi (Moonshot), Qwen (Alibaba Cloud), DeepSeek, Mistral, OpenRouter, Groq ;
- un modèle local (Ollama, LM Studio) ou tout autre service compatible OpenAI (URL libre).

Interfaces : JsonConversation (réponses JSON : CV, lettre, formulaires), ToolConversation (agent navigateur),
complete_text (texte libre). Si « llm_fallback » est coché et que le fournisseur principal échoue au premier appel (clé
refusée, crédit épuisé, service indisponible), les autres fournisseurs configurés prennent le relais, dans l'ordre.
"""
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

import anthropic
import httpx

from . import vault
from .config import DATA, IS_WIN, load_settings, save_settings
from .notify import log

PROVIDERS = {
    "anthropic": {"label": "Claude (clé API Anthropic)", "kind": "anthropic"},
    "claude_code": {"label": "Claude Code (ton abonnement Claude, sans clé)", "kind": "claude_code"},
    "openai": {"label": "ChatGPT (OpenAI)", "kind": "openai", "url": "https://api.openai.com/v1", "key": True},
    "gemini": {"label": "Gemini (Google)", "kind": "openai", "url": "https://generativelanguage.googleapis.com/v1beta/openai", "key": True},
    "kimi": {"label": "Kimi (Moonshot)", "kind": "openai", "url": "https://api.moonshot.ai/v1", "key": True},
    "qwen": {"label": "Qwen (Alibaba Cloud)", "kind": "openai", "url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "key": True},
    "deepseek": {"label": "DeepSeek", "kind": "openai", "url": "https://api.deepseek.com/v1", "key": True},
    "mistral": {"label": "Mistral", "kind": "openai", "url": "https://api.mistral.ai/v1", "key": True},
    "openrouter": {"label": "OpenRouter (nombreux modèles)", "kind": "openai", "url": "https://openrouter.ai/api/v1", "key": True},
    "groq": {"label": "Groq", "kind": "openai", "url": "https://api.groq.com/openai/v1", "key": True},
    "ollama": {"label": "Modèle local : Ollama", "kind": "openai", "url": "http://localhost:11434/v1", "key": False},
    "lmstudio": {"label": "Modèle local : LM Studio", "kind": "openai", "url": "http://localhost:1234/v1", "key": False},
    "custom": {"label": "Autre service compatible OpenAI (URL)", "kind": "openai", "url": "", "key": False},
}
CC_DIR = DATA / "claude_code"  # dossier vide : Claude Code n'y voit aucun de tes fichiers


class ProviderError(Exception):
    pass


# ---------------------------------------------------------------------------------- configuration
def migrate_settings():
    """Anciens réglages (« openai_compatible ») -> un fournisseur nommé, avec sa clé et son modèle."""
    s = load_settings()
    if s.get("llm_provider") != "openai_compatible":
        return
    pid = s.get("foreign_preset") or "custom"
    if pid not in PROVIDERS or s.get("foreign_base_url"):
        pid = "custom"
        s["custom_base_url"] = s.get("foreign_base_url", "")
    s.setdefault("models", {})[pid] = s.get("foreign_model", "")
    s["llm_provider"] = pid
    old_key = vault.get_foreign_key()
    if old_key and not vault.get_provider_key(pid):
        vault.set_provider_key(pid, old_key)
    save_settings(s)


def base_url(pid) -> str:
    s = load_settings()
    url = s.get("custom_base_url", "") if pid == "custom" else PROVIDERS[pid].get("url", "")
    return (url or "").rstrip("/")


def model_of(pid) -> str:
    s = load_settings()
    if pid == "anthropic":
        return s.get("model", "claude-opus-5-5")
    return (s.get("models") or {}).get(pid, "") or ""


def claude_bin():
    """Chemin de la commande « claude » (Claude Code) sur ce PC, ou None."""
    s = load_settings()
    cands = [s.get("claude_code_path") or "", shutil.which("claude") or ""]
    if IS_WIN:
        home = os.path.expanduser("~")
        cands += [os.path.join(home, ".local", "bin", "claude.exe"),
                  os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\claude\claude.exe")]
    return next((c for c in cands if c and os.path.exists(c)), None)


def configured(pid) -> tuple[bool, str]:
    p = PROVIDERS.get(pid)
    if not p:
        return False, "fournisseur inconnu"
    if p["kind"] == "anthropic":
        return (True, "clé enregistrée") if (vault.get_api_key() or os.environ.get("ANTHROPIC_API_KEY")) \
            else (False, "clé API Anthropic à ajouter")
    if p["kind"] == "claude_code":
        exe = claude_bin()
        return (True, f"commande trouvée : {exe}") if exe else \
            (False, "commande « claude » introuvable : installe Claude Code et connecte-toi une fois")
    if not base_url(pid):
        return False, "adresse (URL) du service à indiquer"
    if not model_of(pid):
        return False, "nom du modèle à indiquer"
    if p.get("key") and not vault.get_provider_key(pid):
        return False, "clé API à ajouter"
    return True, f"modèle {model_of(pid)}"


def providers_in_order() -> list[str]:
    s = load_settings()
    primary = s.get("llm_provider", "anthropic")
    if primary not in PROVIDERS:
        primary = "anthropic"
    order = [primary] if configured(primary)[0] else []
    if s.get("llm_fallback", True):
        order += [p for p in PROVIDERS if p != primary and configured(p)[0]]
    return order


def ready() -> bool:
    return bool(providers_in_order())


def describe() -> dict:
    s = load_settings()
    out = {}
    for pid, p in PROVIDERS.items():
        ok, why = configured(pid)
        out[pid] = {"label": p["label"], "kind": p["kind"], "ok": ok, "why": why, "model": model_of(pid),
                    "needs_key": bool(p.get("key")) or p["kind"] == "anthropic",
                    "has_key": bool(vault.get_api_key() if pid == "anthropic" else vault.get_provider_key(pid)),
                    "url": base_url(pid) if p["kind"] == "openai" else ""}
    return {"providers": out, "primary": s.get("llm_provider", "anthropic"), "order": providers_in_order(),
            "fallback": s.get("llm_fallback", True)}


def label(pid) -> str:
    return PROVIDERS.get(pid, {}).get("label", pid)


_anthropic = None


def anthropic_client():
    global _anthropic
    if _anthropic is None:
        key = vault.get_api_key()
        _anthropic = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    return _anthropic


def reset():
    global _anthropic
    _anthropic = None


def _fallback_worthy(e: Exception) -> bool:
    return isinstance(e, (ProviderError, httpx.HTTPError, subprocess.SubprocessError, OSError, TypeError,
                          anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.RateLimitError,
                          anthropic.InternalServerError, anthropic.APIConnectionError, anthropic.NotFoundError)) or \
        (isinstance(e, anthropic.BadRequestError) and re.search(r"credit|billing|balance|model", str(e), re.I) is not None)


def _loads(txt: str):
    txt = re.sub(r"^```(?:json)?\s*|```\s*$", "", (txt or "").strip())
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# ---------------------------------------------------------------------- services compatibles OpenAI
def oa_post(payload: dict, pid: str) -> dict:
    key = vault.get_provider_key(pid)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if pid == "openrouter":
        headers.update({"HTTP-Referer": "http://127.0.0.1:8765", "X-Title": "StageAuto"})
    url = base_url(pid) + "/chat/completions"
    last = ""
    for attempt in range(4):
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=300)
        except httpx.HTTPError as e:
            last = str(e)
            if "localhost" in url or "127.0.0.1" in url:
                raise ProviderError(f"{label(pid)} : service local injoignable ({url}) — est-il lancé ?")
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = f"{r.status_code}"
            time.sleep(4 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise ProviderError(f"{label(pid)} : clé API refusée")
        if r.status_code >= 400:
            raise ProviderError(f"{label(pid)} {r.status_code} : {r.text[:300]}")
        return r.json()
    raise ProviderError(f"{label(pid)} indisponible ({last})")


# ------------------------------------------------------------------------------------- Claude Code
def cc_call(text: str, resume: str | None = None, system: str | None = None, timeout: int = 600) -> tuple[str, str | None]:
    """Une requête à Claude Code en mode non interactif (claude -p). Les consignes de l'application remplacent celles de
    Claude Code (--system-prompt-file), la demande passe par l'entrée standard, et ses outils (fichiers, terminal, web)
    sont désactivés : il ne fait que répondre."""
    exe = claude_bin()
    if not exe:
        raise ProviderError("Claude Code introuvable")
    CC_DIR.mkdir(parents=True, exist_ok=True)
    args = [exe, "-p", "Lis la demande complète transmise sur l'entrée standard et réponds exactement au format demandé.",
            "--output-format", "json",
            "--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,Read,Glob,Grep"]
    if system:
        import hashlib
        f = CC_DIR / f"consignes_{hashlib.sha1(system.encode('utf-8')).hexdigest()[:12]}.txt"
        if not f.exists():
            f.write_text(system, encoding="utf-8")
        args += ["--system-prompt-file", str(f)]
    if model_of("claude_code"):
        args += ["--model", model_of("claude_code")]
    if resume:
        args += ["--resume", resume]
    try:
        r = subprocess.run(args, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=str(CC_DIR), creationflags=0x08000000 if IS_WIN else 0)
    except subprocess.TimeoutExpired:
        raise ProviderError("Claude Code : délai dépassé")
    out = (r.stdout or "").strip()
    j = None
    for line in reversed(out.splitlines()):
        try:
            j = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if j is None:
        j = _loads(out)
    if r.returncode != 0 or not isinstance(j, dict) or j.get("is_error"):
        msg = (j or {}).get("result") if isinstance(j, dict) else None
        raise ProviderError(f"Claude Code : {(msg or r.stderr or out or 'erreur inconnue')[-300:]}")
    return j.get("result") or "", j.get("session_id")


# --------------------------------------------------------------------------------------- JSON
class JsonConversation:
    """Questions successives (ex. CV puis correction) dont chaque réponse est un objet JSON du schéma."""

    def __init__(self, system: str, schema: dict, max_tokens=32000, effort="medium", name="reponse", order=None):
        self.system, self.schema, self.max_tokens, self.effort, self.name = system, schema, max_tokens, effort, name
        self.order = order or providers_in_order()
        self.provider = self.order[0] if self.order else None
        self.msgs: list = []
        self.users: list[str] = []
        self.cc_session = None

    def ask(self, text: str):
        if not self.provider:
            raise ProviderError("aucune IA configurée")
        self.users.append(text)
        self.msgs.append({"role": "user", "content": text})
        try:
            return self._ask()
        except Exception as e:
            if len(self.users) != 1 or not _fallback_worthy(e):
                raise
            for nxt in self.order[self.order.index(self.provider) + 1:]:
                log(f"{label(self.provider)} indisponible ({str(e)[:80]}) : relais par {label(nxt)}")
                self.provider, self.msgs, self.cc_session = nxt, [{"role": "user", "content": text}], None
                try:
                    return self._ask()
                except Exception as e2:
                    if not _fallback_worthy(e2):
                        raise
                    e = e2
            raise e

    def _ask(self):
        kind = PROVIDERS[self.provider]["kind"]
        if kind == "anthropic":
            return self._ask_anthropic()
        if kind == "claude_code":
            return self._ask_claude_code()
        return self._ask_openai()

    def _ask_anthropic(self):
        kw = dict(model=model_of("anthropic"), max_tokens=self.max_tokens,
                  system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                  messages=self.msgs, output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": self.schema}},
                  betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            with anthropic_client().beta.messages.stream(**kw) as st:
                m = st.get_final_message()
        except anthropic.BadRequestError as e:
            if "fallback" not in str(e).lower():
                raise
            kw.pop("betas")
            kw.pop("fallbacks")
            with anthropic_client().messages.stream(**kw) as st:
                m = st.get_final_message()
        self.msgs.append({"role": "assistant", "content": m.content})
        if m.stop_reason == "refusal":
            return None
        return _loads("".join(b.text for b in m.content if b.type == "text"))

    def _ask_openai(self):
        s = load_settings()
        base = {"model": model_of(self.provider), "messages": [{"role": "system", "content": self.system}] + self.msgs,
                "max_tokens": min(self.max_tokens, int(s.get("foreign_max_tokens", 16000)))}
        try:
            j = oa_post({**base, "response_format": {"type": "json_schema",
                                                     "json_schema": {"name": self.name, "schema": self.schema, "strict": True}}},
                        self.provider)
        except ProviderError as e:
            if not re.search(r"response_format|json_schema|400|422", str(e)):
                raise
            sysmsg = self.system + "\n\nRéponds UNIQUEMENT par un objet JSON conforme à ce schéma :\n" + json.dumps(self.schema, ensure_ascii=False)
            try:
                j = oa_post({**base, "messages": [{"role": "system", "content": sysmsg}] + self.msgs,
                             "response_format": {"type": "json_object"}}, self.provider)
            except ProviderError as e2:
                if not re.search(r"response_format|400|422", str(e2)):
                    raise
                j = oa_post({**base, "messages": [{"role": "system", "content": sysmsg}] + self.msgs}, self.provider)
        txt = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        self.msgs.append({"role": "assistant", "content": txt})
        return _loads(txt)

    def _ask_claude_code(self):
        text = self.msgs[-1]["content"]
        if self.cc_session is not None:
            text += "\n\nRéponds uniquement par l'objet JSON complet, conforme au même schéma."
        system = (f"{self.system}\n\n=====\nRéponds UNIQUEMENT par un objet JSON (sans texte autour, sans bloc de code) "
                  f"conforme à ce schéma :\n{json.dumps(self.schema, ensure_ascii=False)}")
        result, sid = cc_call(text, resume=self.cc_session, system=system)
        self.cc_session = sid or self.cc_session
        self.msgs.append({"role": "assistant", "content": result})
        return _loads(result)


def complete_text(prompt: str, max_tokens=32000) -> str | None:
    for pid in providers_in_order():
        try:
            kind = PROVIDERS[pid]["kind"]
            if kind == "anthropic":
                with anthropic_client().messages.stream(model=model_of("anthropic"), max_tokens=max_tokens,
                                                        messages=[{"role": "user", "content": prompt}]) as st:
                    m = st.get_final_message()
                return "".join(b.text for b in m.content if b.type == "text")
            if kind == "claude_code":
                return cc_call(prompt)[0]
            s = load_settings()
            j = oa_post({"model": model_of(pid), "messages": [{"role": "user", "content": prompt}],
                         "max_tokens": min(max_tokens, int(s.get("foreign_max_tokens", 16000)))}, pid)
            return ((j.get("choices") or [{}])[0].get("message") or {}).get("content")
        except Exception as e:
            log(f"{label(pid)} : {e.__class__.__name__} {str(e)[:100]}")
    return None


def test(pid: str) -> dict:
    """Bouton « Tester » : une petite question à ce fournisseur."""
    ok, why = configured(pid)
    if not ok:
        return {"ok": False, "error": why}
    t0 = time.time()
    try:
        conv = JsonConversation("Tu réponds en JSON.", {"type": "object", "additionalProperties": False, "required": ["ok", "mot"],
                                                         "properties": {"ok": {"type": "boolean"}, "mot": {"type": "string"}}},
                                max_tokens=2000, effort="low", name="test", order=[pid])
        d = conv.ask("Réponds ok=true et mot=\"bonjour\".")
        if not d:
            return {"ok": False, "error": "réponse illisible"}
        return {"ok": True, "answer": d, "seconds": round(time.time() - t0, 1)}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__} : {str(e)[:250]}"}


# ------------------------------------------------------------------------------- outils (agent)
@dataclass
class Call:
    id: str
    name: str
    input: dict


@dataclass
class Step:
    calls: list = field(default_factory=list)
    text: str = ""
    refusal: bool = False


class ToolConversation:
    def __init__(self, system: str, tools: list, effort="low", order=None):
        self.system, self.tools, self.effort = system, tools, effort
        self.order = order or providers_in_order()
        self.provider = self.order[0] if self.order else None
        self.msgs: list = []
        self.first_user = None
        self.turns = 0
        self._flags = {"fallbacks": True, "ctx": True}
        self.cc_session = None
        self.cc_pending: list[str] = []
        self.cc_n = 0

    def user(self, content):
        if self.first_user is None:
            self.first_user = content
        self.msgs.append({"role": "user", "content": content})
        self.cc_pending.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))

    def step(self) -> Step:
        if not self.provider:
            raise ProviderError("aucune IA configurée")
        try:
            st = self._step()
        except Exception as e:
            if self.turns != 0 or not _fallback_worthy(e):
                raise
            st = None
            for nxt in self.order[self.order.index(self.provider) + 1:]:
                log(f"{label(self.provider)} indisponible ({str(e)[:80]}) : relais par {label(nxt)}")
                self.provider = nxt
                self.msgs = [{"role": "user", "content": self.first_user}]
                self.cc_session, self.cc_pending = None, [self.first_user]
                try:
                    st = self._step()
                    break
                except Exception as e2:
                    if not _fallback_worthy(e2):
                        raise
                    e = e2
            if st is None:
                raise e
        self.turns += 1
        return st

    def _step(self) -> Step:
        kind = PROVIDERS[self.provider]["kind"]
        if kind == "anthropic":
            return self._step_anthropic()
        if kind == "claude_code":
            return self._step_claude_code()
        return self._step_openai()

    def results(self, results: list):
        """results : [{id, content (str ou liste avec image), is_error}]"""
        kind = PROVIDERS[self.provider]["kind"] if self.provider else "anthropic"
        if kind == "anthropic":
            self.msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                                                           "is_error": bool(r.get("is_error"))} for r in results]})
            return
        if kind == "claude_code":
            lines = []
            for r in results:
                c = r["content"]
                if isinstance(c, list):
                    c = "(capture d'écran non disponible avec Claude Code : utilise read_page ou find)"
                lines.append(f"[{r.get('name', r['id'])}]{' ERREUR' if r.get('is_error') else ''} :\n{c}")
            self.cc_pending.append("RÉSULTATS DES OUTILS :\n\n" + "\n\n".join(lines))
            return
        images = []
        for r in results:
            c = r["content"]
            if isinstance(c, list):
                images += [b for b in c if b.get("type") == "image"]
                c = "capture jointe ci-dessous"
            self.msgs.append({"role": "tool", "tool_call_id": r["id"], "content": ("ERREUR : " if r.get("is_error") else "") + str(c)})
        if images:
            self.msgs.append({"role": "user", "content": [{"type": "text", "text": "Capture d'écran :"}] + [
                {"type": "image_url", "image_url": {"url": f"data:{b['source']['media_type']};base64,{b['source']['data']}"}}
                for b in images]})
        self._prune()

    # -- Claude (API)
    def _step_anthropic(self) -> Step:
        kw = dict(model=model_of("anthropic"), max_tokens=8000,
                  system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                  tools=self.tools, messages=self.msgs, cache_control={"type": "ephemeral"},
                  output_config={"effort": self.effort})
        betas = []
        if self._flags["fallbacks"]:
            betas.append("server-side-fallback-2026-07-01")
            kw["fallbacks"] = "default"
        if self._flags["ctx"]:
            betas.append("context-management-2025-06-27")
            kw["context_management"] = {"edits": [{"type": "clear_tool_uses_20250919"}]}
        if betas:
            kw["betas"] = betas
        try:
            resp = anthropic_client().beta.messages.create(**kw)
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            if self._flags["fallbacks"] and "fallback" in msg:
                self._flags["fallbacks"] = False
                return self._step_anthropic()
            if self._flags["ctx"] and any(k in msg for k in ("context_management", "context-management", "clear_tool")):
                self._flags["ctx"] = False
                return self._step_anthropic()
            raise
        self.msgs.append({"role": "assistant", "content": resp.content})
        calls = [Call(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"]
        text = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text")
        return Step(calls, text, resp.stop_reason == "refusal")

    # -- services compatibles OpenAI
    def _oa_tools(self):
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["input_schema"]}} for t in self.tools]

    def _step_openai(self) -> Step:
        s = load_settings()
        j = oa_post({"model": model_of(self.provider), "messages": [{"role": "system", "content": self.system}] + self.msgs,
                     "tools": self._oa_tools(), "tool_choice": "auto",
                     "max_tokens": min(8000, int(s.get("foreign_max_tokens", 16000)))}, self.provider)
        msg = ((j.get("choices") or [{}])[0].get("message") or {})
        keep = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            keep["tool_calls"] = msg["tool_calls"]
        self.msgs.append(keep)
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(Call(tc.get("id") or f"call{len(calls)}", fn.get("name", ""), args))
        return Step(calls, msg.get("content") or "", (j.get("choices") or [{}])[0].get("finish_reason") == "content_filter")

    def _prune(self, keep=6):
        """Pas d'édition de contexte côté serveur : vide les vieux résultats d'outils."""
        idx = [i for i, m in enumerate(self.msgs) if m.get("role") == "tool"]
        for i in idx[:-keep]:
            if len(self.msgs[i]["content"]) > 200:
                self.msgs[i]["content"] = "(ancien résultat effacé)"

    # -- Claude Code : outils décrits en texte, appels rendus en JSON, session reprise à chaque étape
    def _catalog(self) -> str:
        lines = []
        for t in self.tools:
            props = t["input_schema"].get("properties", {})
            params = ", ".join(f"{k} ({v.get('type', 'objet')}{': ' + '|'.join(v['enum']) if v.get('enum') else ''})"
                               for k, v in props.items())
            lines.append(f"- {t['name']}({params}) : {t['description']}")
        return "\n".join(lines)

    def _step_claude_code(self) -> Step:
        body = "\n\n".join(self.cc_pending)
        self.cc_pending = []
        if self.cc_session is not None:
            body += '\n\nRéponds uniquement par le JSON {"calls": [...]} des prochains appels.'
        system = (f"{self.system}\n\nOUTILS DISPONIBLES (tu ne peux agir qu'avec eux) :\n{self._catalog()}\n\n"
                  "FORMAT DE CHAQUE RÉPONSE : uniquement un objet JSON, sans texte autour :\n"
                  '{"calls": [{"name": "<outil>", "input": {<paramètres>}}]}\n'
                  "Plusieurs appels possibles dans l'ordre. Termine toujours la tâche avec l'outil finish.")
        result, sid = cc_call(body, resume=self.cc_session, system=system)
        self.cc_session = sid or self.cc_session
        d = _loads(result)
        calls = []
        for c in (d or {}).get("calls") or []:
            if isinstance(c, dict) and c.get("name"):
                self.cc_n += 1
                calls.append(Call(f"cc{self.cc_n}", c["name"], c.get("input") or {}))
        return Step(calls, result if not calls else "", False)
