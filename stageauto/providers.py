"""Fournisseurs d'IA : Claude (par défaut) ou tout service compatible OpenAI avec ta propre clé
(OpenAI, Mistral, Google Gemini, DeepSeek, OpenRouter, Groq…). Même interface pour tout le reste de l'application :

- JsonConversation : réponse JSON conforme à un schéma (CV, lettre, réponses aux formulaires) ;
- ToolConversation : boucle d'outils de l'agent navigateur ;
- complete_text : texte libre (traduction).

Si « llm_fallback » est activé et que le fournisseur principal échoue dès le premier appel (clé refusée, crédit épuisé,
service indisponible), l'autre fournisseur configuré prend le relais.
"""
import json
import re
import time
from dataclasses import dataclass, field

import anthropic
import httpx

from . import vault
from .config import load_settings
from .notify import log

PRESETS = {
    "openai": "https://api.openai.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
}


class ProviderError(Exception):
    pass


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


def foreign_ready() -> bool:
    s = load_settings()
    return bool(vault.get_foreign_key() and s.get("foreign_model") and (s.get("foreign_base_url") or s.get("foreign_preset")))


def providers_in_order() -> list[str]:
    s = load_settings()
    first = s.get("llm_provider", "anthropic")
    if first == "openai_compatible" and not foreign_ready():
        first = "anthropic"
    order = [first]
    other = "anthropic" if first == "openai_compatible" else "openai_compatible"
    if s.get("llm_fallback", True) and (other == "anthropic" and vault.get_api_key() or other == "openai_compatible" and foreign_ready()):
        order.append(other)
    return order


def _fallback_worthy(e: Exception) -> bool:
    return isinstance(e, (ProviderError, anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                          anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError)) or \
        (isinstance(e, anthropic.BadRequestError) and re.search(r"credit|billing|balance", str(e), re.I) is not None)


# ------------------------------------------------------------------------------ OpenAI-compatible
def _base_url():
    s = load_settings()
    return (s.get("foreign_base_url") or PRESETS.get(s.get("foreign_preset", ""), "")).rstrip("/")


def oa_post(payload: dict) -> dict:
    key = vault.get_foreign_key()
    url = _base_url() + "/chat/completions"
    last = ""
    for attempt in range(4):
        try:
            r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {key}"}, timeout=240)
        except httpx.HTTPError as e:
            last = str(e)
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = f"{r.status_code}"
            time.sleep(4 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise ProviderError("clé API du fournisseur externe refusée")
        if r.status_code >= 400:
            raise ProviderError(f"fournisseur externe {r.status_code} : {r.text[:300]}")
        return r.json()
    raise ProviderError(f"fournisseur externe indisponible ({last})")


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


# ------------------------------------------------------------------------------ JSON
class JsonConversation:
    """Questions successives (ex. CV puis correction) dont chaque réponse est un objet JSON du schéma."""

    def __init__(self, system: str, schema: dict, max_tokens=32000, effort="medium", name="reponse"):
        self.system, self.schema, self.max_tokens, self.effort, self.name = system, schema, max_tokens, effort, name
        self.order = providers_in_order()
        self.provider = self.order[0]
        self.msgs: list = []
        self.users: list[str] = []

    def ask(self, text: str):
        self.users.append(text)
        self.msgs.append({"role": "user", "content": text})
        try:
            return self._ask()
        except Exception as e:
            if len(self.users) == 1 and len(self.order) > 1 and _fallback_worthy(e):
                log(f"{self.provider} indisponible ({e.__class__.__name__}) : relais par {self.order[1]}")
                self.provider = self.order[1]
                self.msgs = [{"role": "user", "content": text}]
                return self._ask()
            raise

    def _ask(self):
        if self.provider == "anthropic":
            return self._ask_anthropic()
        return self._ask_openai()

    def _ask_anthropic(self):
        s = load_settings()
        kw = dict(model=s.get("model", "claude-opus-5-5"), max_tokens=self.max_tokens,
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
        base = {"model": s.get("foreign_model"), "messages": [{"role": "system", "content": self.system}] + self.msgs,
                "max_tokens": min(self.max_tokens, int(s.get("foreign_max_tokens", 16000)))}
        try:
            j = oa_post({**base, "response_format": {"type": "json_schema",
                                                     "json_schema": {"name": self.name, "schema": self.schema, "strict": True}}})
        except ProviderError as e:
            if "response_format" not in str(e) and "json_schema" not in str(e) and "400" not in str(e):
                raise
            sysmsg = self.system + "\n\nRéponds UNIQUEMENT par un objet JSON conforme à ce schéma :\n" + json.dumps(self.schema, ensure_ascii=False)
            j = oa_post({**base, "messages": [{"role": "system", "content": sysmsg}] + self.msgs,
                         "response_format": {"type": "json_object"}})
        txt = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        self.msgs.append({"role": "assistant", "content": txt})
        return _loads(txt)


def complete_text(prompt: str, max_tokens=32000) -> str | None:
    for prov in providers_in_order():
        try:
            if prov == "anthropic":
                s = load_settings()
                with anthropic_client().messages.stream(model=s.get("model", "claude-opus-5-5"), max_tokens=max_tokens,
                                                        messages=[{"role": "user", "content": prompt}]) as st:
                    m = st.get_final_message()
                return "".join(b.text for b in m.content if b.type == "text")
            s = load_settings()
            j = oa_post({"model": s.get("foreign_model"), "messages": [{"role": "user", "content": prompt}],
                         "max_tokens": min(max_tokens, int(s.get("foreign_max_tokens", 16000)))})
            return ((j.get("choices") or [{}])[0].get("message") or {}).get("content")
        except Exception as e:
            log(f"{prov} : {e.__class__.__name__}")
    return None


# ------------------------------------------------------------------------------ outils (agent)
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
    def __init__(self, system: str, tools: list, effort="low"):
        self.system, self.tools, self.effort = system, tools, effort
        self.order = providers_in_order()
        self.provider = self.order[0]
        self.msgs: list = []
        self.first_user = None
        self.turns = 0
        self._flags = {"fallbacks": True, "ctx": True}

    def user(self, content):
        if self.first_user is None:
            self.first_user = content
        self.msgs.append({"role": "user", "content": content})

    def step(self) -> Step:
        try:
            st = self._step_anthropic() if self.provider == "anthropic" else self._step_openai()
        except Exception as e:
            if self.turns == 0 and len(self.order) > 1 and _fallback_worthy(e):
                log(f"{self.provider} indisponible ({e.__class__.__name__}) : relais par {self.order[1]}")
                self.provider = self.order[1]
                self.msgs = [{"role": "user", "content": self.first_user}]
                st = self._step_anthropic() if self.provider == "anthropic" else self._step_openai()
            else:
                raise
        self.turns += 1
        return st

    def results(self, results: list):
        """results : [{id, content (str ou liste avec image), is_error}]"""
        if self.provider == "anthropic":
            self.msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                                                           "is_error": bool(r.get("is_error"))} for r in results]})
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

    # -- Claude
    def _step_anthropic(self) -> Step:
        s = load_settings()
        kw = dict(model=s.get("model", "claude-opus-5-5"), max_tokens=8000,
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

    # -- OpenAI-compatible
    def _oa_tools(self):
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["input_schema"]}} for t in self.tools]

    def _step_openai(self) -> Step:
        s = load_settings()
        j = oa_post({"model": s.get("foreign_model"), "messages": [{"role": "system", "content": self.system}] + self.msgs,
                     "tools": self._oa_tools(), "tool_choice": "auto",
                     "max_tokens": min(8000, int(s.get("foreign_max_tokens", 16000)))})
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
        """Fournisseur externe (pas d'édition de contexte côté serveur) : vide les vieux résultats d'outils."""
        idx = [i for i, m in enumerate(self.msgs) if m.get("role") == "tool"]
        for i in idx[:-keep]:
            if len(self.msgs[i]["content"]) > 200:
                self.msgs[i]["content"] = "(ancien résultat effacé)"
