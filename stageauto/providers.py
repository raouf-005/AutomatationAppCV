"""Fournisseurs d'IA au choix, même interface pour toute l'application :

- Claude (clé API Anthropic) ;
- Claude Code : la commande « claude » installée sur ton PC, avec ton abonnement Claude (pas de clé API) ;
- ChatGPT (OpenAI), Gemini (Google), Kimi (Moonshot), Qwen (Alibaba Cloud), DeepSeek, Mistral, OpenRouter, Groq ;
- un modèle local (Ollama, LM Studio) ou tout autre service compatible OpenAI (URL libre).

Interfaces : JsonConversation (réponses JSON : CV, lettre, formulaires), ToolConversation (agent navigateur),
complete_text (texte libre). Si « llm_fallback » est coché et que le fournisseur principal échoue au premier appel (clé
refusée, crédit épuisé, service indisponible), les autres fournisseurs configurés prennent le relais, dans l'ordre.
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import threading
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
    m = (s.get("models") or {}).get(pid, "") or ""
    if pid == "claude_code" and m in ("", None):
        return "sonnet"  # Claude Code : Sonnet par défaut (rapide, n'épuise pas les limites d'Opus) ; réglable dans Réglages → IA
    if m == "défaut":
        return ""
    if not m and pid == "ollama" and s.get("local_backup", True):
        m = ollama_auto_model()
    return m


LOCAL = ("ollama", "lmstudio")
_OLLAMA = {"t": 0.0, "models": []}
# modèles capables de JSON et d'outils, du plus adapté au moins adapté (le plus gros d'une même famille d'abord)
# « stageauto » : modèle réglé pour l'application (ollama/Modelfile, créé par installer_modele_local.bat) ; puis les
# familles qui suivent le mieux un JSON et des outils, y compris en petite taille (4 milliards de paramètres)
_OLLAMA_PREF = ("stageauto", "hf.co/jackrong/deepseek-v4-pro-qwen3.5", "qwen3.5", "qwen3", "qwen2.5", "gemma3", "llama3.3", "llama3.1", "llama3.2", "mistral-small", "mistral-nemo",
                "phi4-mini", "phi4", "mistral", "llama3")


def ollama_models() -> list:
    """Modèles installés dans Ollama (http://localhost:11434), relus toutes les 2 min ; vide si Ollama n'est pas lancé."""
    if time.time() - _OLLAMA["t"] < 120:
        return _OLLAMA["models"]
    try:
        r = httpx.get(base_url("ollama").replace("/v1", "") + "/api/tags", timeout=1.5)
        _OLLAMA["models"] = [m["name"] for m in r.json().get("models", []) if "embed" not in m["name"]]
    except Exception:
        _OLLAMA["models"] = []
    _OLLAMA["t"] = time.time()
    return _OLLAMA["models"]


def ollama_auto_model() -> str:
    """Modèle Ollama choisi tout seul quand aucun n'est indiqué : la famille la plus adaptée, la plus grosse taille."""
    ms = ollama_models()
    if not ms:
        return ""
    def size(n):
        m = re.search(r":(\d+(?:\.\d+)?)b", n)
        return float(m.group(1)) if m else 0
    for fam in _OLLAMA_PREF:
        cand = [m for m in ms if m.lower().startswith(fam)]
        if cand:
            return max(cand, key=size)
    return max(ms, key=size)


_CC = {"t": 0.0, "key": None, "exe": None, "checked": [], "deep": False}
_VALID: dict = {}
_CC_LOCK = threading.Lock()
INSTALL_HINT = ("Dans un terminal, tape « where claude » (PowerShell : (Get-Command claude).Source) et colle le chemin "
                "affiché dans « Chemin de la commande claude ». Si la commande n'existe pas (Claude Code utilisé seulement "
                "dans l'application de bureau), installe la version terminal : PowerShell → irm https://claude.ai/install.ps1 | iex, "
                "puis tape claude une fois pour te connecter.")
# Dans WSL : Claude Code installé par l'installateur, par npm ou par nvm (que « bash -lc » ne charge pas toujours)
_WSL_FIND = ('for c in "$(command -v claude 2>/dev/null)" "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" '
             '$HOME/.nvm/versions/node/*/bin/claude /usr/local/bin/claude; do '
             'if [ -n "$c" ] && [ -x "$c" ]; then PATH="$(dirname "$c"):$PATH" exec "$c" "$@"; fi; done; '
             'echo "claude introuvable dans WSL" >&2; exit 127')


def _registry_path_dirs() -> list:
    """PATH actuel de Windows (lu dans le registre) : un Claude Code installé après le démarrage de StageAuto est vu."""
    if not IS_WIN:
        return []
    import winreg
    dirs = []
    for root, sub in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, sub) as k:
                v, _ = winreg.QueryValueEx(k, "Path")
                dirs += [os.path.expandvars(d.strip()) for d in str(v).split(";") if d.strip()]
        except OSError:
            pass
    return dirs


def _gui_app(path: str) -> bool:
    """Application de bureau Claude (fenêtre) et non l'outil en ligne de commande : on ne la lance jamais pour un test.
    Electron (resources/app.asar à côté), installation Squirrel (AnthropicClaude), alias du Microsoft Store (WindowsApps)."""
    k = os.path.normcase(path).replace(" ", "")
    return ("anthropicclaude" in k or os.sep + "windowsapps" + os.sep in k
            or os.path.exists(os.path.join(os.path.dirname(path), "resources", "app.asar")))


def _newest_first(paths: list) -> list:
    def mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0
    return sorted(paths, key=mtime, reverse=True)


def _candidates() -> list:
    import glob
    s = load_settings()
    c = []
    if s.get("claude_code_path"):
        c.append(s["claude_code_path"].strip().strip('"'))
    if shutil.which("claude"):
        c.append(shutil.which("claude"))
    names = ["claude.exe", "claude.cmd"] if IS_WIN else ["claude"]
    for d in _registry_path_dirs():
        c += [os.path.join(d, n) for n in names]
    home = os.path.expanduser("~")
    if IS_WIN:
        la, ra = os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")
        c += [os.path.join(home, ".local", "bin", "claude.exe"),            # installateur officiel (install.ps1)
              os.path.join(home, ".claude", "local", "claude.exe"),
              os.path.join(home, ".claude", "local", "claude.cmd"),
              os.path.join(ra, "npm", "claude.cmd"),                         # npm
              os.path.join(la, "Microsoft", "WinGet", "Links", "claude.exe"),  # winget
              os.path.join(home, "scoop", "shims", "claude.exe"),
              os.path.join(la, "Volta", "bin", "claude.exe"),
              os.path.join(la, "pnpm", "claude.cmd"), os.path.join(ra, "pnpm", "claude.cmd")]
        for pat in (os.path.join(la, "Microsoft", "WinGet", "Packages", "Anthropic.ClaudeCode*", "**", "claude.exe"),
                    os.path.join(ra, "Claude", "claude-code", "*", "claude*.exe"),     # copie de l'application de bureau Claude
                    os.path.join(la, "Claude", "claude-code", "*", "claude*.exe"),
                    os.path.join(ra, "nvm", "*", "claude.cmd")):
            c += _newest_first(glob.glob(pat, recursive=True))
    else:
        c += [os.path.join(home, ".local", "bin", "claude"), os.path.join(home, ".claude", "local", "claude"),
              "/usr/local/bin/claude", "/opt/homebrew/bin/claude"]
    out, seen = [], set()
    for x in c:
        k = os.path.normcase(x or "")
        if x and k not in seen and os.path.isfile(x) and not _gui_app(x):
            seen.add(k)
            out.append(x)
    return out


def _is_claude_code(exe: str) -> bool:
    if exe not in _VALID:
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=25, encoding="utf-8",
                               errors="replace", creationflags=0x08000000 if IS_WIN else 0)
            _VALID[exe] = "claude code" in (r.stdout + r.stderr).lower()
        except Exception:
            _VALID[exe] = False
    return _VALID[exe]


def _wsl_has_claude() -> bool:
    if not IS_WIN or not shutil.which("wsl"):
        return False
    if "wsl" not in _VALID:
        try:
            r = subprocess.run(["wsl.exe", "-e", "bash", "-lc", _WSL_FIND, "claude", "--version"], capture_output=True,
                               timeout=40, creationflags=0x08000000)
            out = (r.stdout + r.stderr).decode("utf-8", "ignore").replace("\x00", "")  # wsl.exe peut écrire en UTF-16
            _VALID["wsl"] = "claude code" in out.lower()
        except Exception:
            _VALID["wsl"] = False
    return _VALID["wsl"]


def _fresh(key: str, deep: bool) -> bool:
    return (_CC["key"] == key and time.time() - _CC["t"] < 120
            and (bool(_CC["exe"]) or _CC["deep"] or not deep))


def _scan(key: str, deep: bool, refresh: bool, release: bool = False):
    try:
        if refresh:
            _VALID.clear()
        found, checked = None, []
        for exe in _candidates():
            ok = _is_claude_code(exe)
            checked.append({"path": exe, "ok": ok})
            if ok:
                found = exe
                break
        if not found and deep and _wsl_has_claude():
            found = "wsl"
            checked.append({"path": "WSL (Linux sous Windows)", "ok": True})
        _CC.update(t=time.time(), key=key, exe=found, checked=checked, deep=deep)
    except Exception as e:
        log(f"recherche de Claude Code : {e.__class__.__name__} {str(e)[:120]}")
    finally:
        if release:
            _CC_LOCK.release()


def claude_bin(refresh: bool = False, wait: bool = True, deep: bool = False):
    """Commande Claude Code utilisable sur ce PC (chemin, ou « wsl »), ou None. Résultat gardé 2 minutes.
    wait=False (tableau de bord) : ne bloque jamais ; la recherche se fait en arrière-plan et la réponse est le dernier
    résultat connu. deep : cherche aussi dans WSL (démarre WSL, lent) ; d'office si Claude Code est l'IA choisie."""
    s = load_settings()
    key = s.get("claude_code_path", "")
    deep = deep or refresh or s.get("llm_provider") == "claude_code"
    if not refresh and _fresh(key, deep):
        return _CC["exe"]
    if not wait:
        if _CC_LOCK.acquire(blocking=False):
            threading.Thread(target=_scan, args=(key, deep, refresh, True), daemon=True).start()
        return _CC["exe"] if _CC["key"] == key else None
    with _CC_LOCK:
        if refresh or not _fresh(key, deep):
            _scan(key, deep, refresh)
    return _CC["exe"]


def claude_searching() -> bool:
    return _CC_LOCK.locked()


def claude_diag() -> dict:
    """Bouton « Rechercher Claude Code » : ce qui a été trouvé et où."""
    exe = claude_bin(refresh=True)
    return {"found": exe, "checked": _CC["checked"], "hint": "" if exe else INSTALL_HINT}


def _wsl_path(p: str) -> str:
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", str(p))
    return f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}" if m else str(p)


def _cc_env() -> dict:
    """Claude Code sous Windows a besoin de Git Bash : s'il est installé mais absent du PATH de StageAuto, on l'indique."""
    env = dict(os.environ)
    if IS_WIN and not env.get("CLAUDE_CODE_GIT_BASH_PATH") and not shutil.which("bash"):
        for b in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe",
                  os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "bin", "bash.exe")):
            if os.path.isfile(b):
                env["CLAUDE_CODE_GIT_BASH_PATH"] = b
                break
    return env


def _login_hint(exe: str) -> str:
    if exe == "wsl":
        return " — Claude Code n'est pas connecté : ouvre WSL (Ubuntu), tape claude, puis /login."
    if shutil.which("claude") and os.path.normcase(shutil.which("claude")) == os.path.normcase(exe):
        return " — Claude Code n'est pas connecté : ouvre un terminal, tape claude, puis /login."
    return f" — Claude Code n'est pas connecté : ouvre l'invite de commandes, colle \"{exe}\" puis Entrée, et tape /login."


def configured(pid, wait: bool = True) -> tuple[bool, str]:
    p = PROVIDERS.get(pid)
    if not p:
        return False, "fournisseur inconnu"
    if refused(pid):
        return False, refused(pid)
    if limited(pid):
        return False, limited(pid)
    if p["kind"] == "anthropic":
        return (True, "clé enregistrée") if (vault.get_api_key() or os.environ.get("ANTHROPIC_API_KEY")) \
            else (False, "clé API Anthropic à ajouter")
    if p["kind"] == "claude_code":
        exe = claude_bin(wait=wait)
        if not exe and claude_searching():
            return False, "recherche de Claude Code sur ce PC…"
        if exe == "wsl":
            return True, "Claude Code trouvé dans WSL (Linux sous Windows)"
        return (True, f"Claude Code trouvé : {exe}") if exe else \
            (False, "Claude Code introuvable sur ce PC : clique « Rechercher Claude Code » ou indique le chemin de la commande")
    if not base_url(pid):
        return False, "adresse (URL) du service à indiquer"
    if pid == "ollama" and not (load_settings().get("models") or {}).get("ollama"):
        m = model_of(pid)
        if not m:
            return False, ("Ollama non lancé sur ce PC (ou aucun modèle installé : ollama pull qwen2.5:7b)"
                           if load_settings().get("local_backup", True) else "nom du modèle à indiquer")
        return True, f"modèle {m} (choisi tout seul, dernier recours)"
    if not model_of(pid):
        return False, "nom du modèle à indiquer"
    if p.get("key") and not vault.get_provider_key(pid):
        return False, "clé API à ajouter"
    return True, f"modèle {model_of(pid)}"


def providers_in_order(wait: bool = True, task: str = "") -> list[str]:
    """Ordre d'essai des IA. task="apply" : l'IA choisie pour postuler (réglage apply_provider) passe en tête ;
    task="cv" : celle choisie pour les CV et lettres (cv_provider). Sans choix : l'IA principale."""
    s = load_settings()
    primary = s.get("llm_provider", "anthropic")
    if primary not in PROVIDERS:
        primary = "anthropic"
    order = [primary] if configured(primary, wait)[0] else []
    if s.get("llm_fallback", True):
        order += [p for p in PROVIDERS if p != primary and p not in LOCAL and configured(p, wait)[0]]
    # modèle local (Ollama, LM Studio) : toujours en dernier recours, même sans relais activé, s'il tourne sur le PC
    if s.get("llm_fallback", True) or s.get("local_backup", True):
        order += [p for p in LOCAL if p not in order and configured(p, wait)[0]]
    chosen = s.get(f"{task}_provider") if task else ""
    if chosen and chosen in PROVIDERS and chosen != (order[0] if order else None) and configured(chosen, wait)[0]:
        order = [chosen] + [p for p in order if p != chosen]
    return order


def ready(wait: bool = True) -> bool:
    return bool(providers_in_order(wait))


def describe(wait: bool = True) -> dict:
    s = load_settings()
    out = {}
    for pid, p in PROVIDERS.items():
        ok, why = configured(pid, wait)
        out[pid] = {"label": p["label"], "kind": p["kind"], "ok": ok, "why": why, "model": model_of(pid),
                    "needs_key": bool(p.get("key")) or p["kind"] == "anthropic",
                    "has_key": bool(vault.get_api_key() if pid == "anthropic" else vault.get_provider_key(pid)),
                    "url": base_url(pid) if p["kind"] == "openai" else ""}
    ap, cv = providers_in_order(wait, "apply"), providers_in_order(wait, "cv")
    return {"providers": out, "primary": s.get("llm_provider", "anthropic"), "order": providers_in_order(wait),
            "apply": ap[0] if ap else "", "cv": cv[0] if cv else "",
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
    _REFUSED.clear()


# Clé refusée (401/403) : le fournisseur est écarté 30 min (ou jusqu'à une nouvelle clé) au lieu d'échouer offre par offre
_REFUSED: dict = {}
# Limite d'utilisation atteinte (Claude Code : limite de 5 h, quotidienne ou hebdomadaire ; API : quota, 429) : le
# fournisseur est écarté jusqu'à l'heure de réinitialisation annoncée ; les autres IA prennent le relais
_LIMITED: dict = {}
LIMIT_RE = re.compile(r"usage limit|limit reached|hit your limit|reached your (usage )?limit|rate.?limit|quota|"
                      r"resource.?exhausted|too many requests|\b429\b|credit balance|limite d.utilisation", re.I)
_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def is_limit_error(e: Exception) -> bool:
    if isinstance(e, anthropic.RateLimitError):
        return True
    return bool(LIMIT_RE.search(str(e))) and not _auth_error(e)


def reset_time(e: Exception) -> dt.datetime:
    """Heure de réinitialisation annoncée par le message (« resets 3pm », « resets Nov 7, 4pm », « |1748127600 »,
    « in 2 hours », en-tête retry-after), sinon un nouvel essai dans 30 min."""
    now = dt.datetime.now()
    msg = str(e)
    resp = getattr(e, "response", None)
    ra = resp.headers.get("retry-after") if resp is not None and hasattr(resp, "headers") else None
    if ra and str(ra).strip().isdigit():
        return now + dt.timedelta(seconds=int(ra) + 30)
    m = re.search(r"\|\s*(\d{10})\b", msg)
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1))) + dt.timedelta(minutes=2)
    m = re.search(r"in (\d+) ?(h|hours?|min|minutes?)\b", msg, re.I)
    if m:
        n = int(m.group(1))
        return now + (dt.timedelta(hours=n) if m.group(2).lower().startswith("h") else dt.timedelta(minutes=n)) + dt.timedelta(minutes=2)
    m = re.search(r"resets?(?: at| on)?\s+(?:([A-Za-z]{3})[a-z]*\.? (\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?:[:h](\d{2}))?\s*(am|pm)?",
                  msg, re.I)
    if m:
        h, mi = int(m.group(3)), int(m.group(4) or 0)
        ap = (m.group(5) or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if h < 24 and mi < 60:
            day = now.date()
            if m.group(1) and m.group(1).lower()[:3] in _MONTHS:
                try:
                    day = dt.date(now.year, _MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
                except ValueError:
                    pass
            t = dt.datetime.combine(day, dt.time(h, mi))
            while t <= now:
                t += dt.timedelta(days=1)
            return t + dt.timedelta(minutes=2)
    return now + dt.timedelta(minutes=30)


def limited(pid: str) -> str:
    r = _LIMITED.get(pid)
    if r and dt.datetime.now() < r[0]:
        return r[1]
    _LIMITED.pop(pid, None)
    return ""


def next_reset():
    """Plus proche réinitialisation parmi les IA en pause (pour relancer tout seul), ou None."""
    live = [r[0] for pid, r in _LIMITED.items() if limited(pid)]
    return min(live) if live else None


def _auth_error(e: Exception) -> bool:
    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return True
    return isinstance(e, ProviderError) and re.search(
        r"\b(401|403)\b|invalid (api key|bearer|x-api-key)|incorrect api key|unauthori[sz]ed|authentication_error",
        str(e), re.I) is not None


def note_failure(pid: str, e: Exception) -> None:
    if pid in LOCAL:
        return  # modèle local : ni limite ni clé ; une erreur ne le met jamais en pause
    if pid and is_limit_error(e) and not limited(pid):
        until = reset_time(e)
        others = [p for p in providers_in_order() if p != pid]
        msg = f"limite atteinte ({label(pid)}) : reprise vers {until:%H:%M}" + ("" if until.date() == dt.date.today() else f" le {until:%d/%m}")
        _LIMITED[pid] = (until, msg)
        log(msg + (f" — relais par {label(others[0])}" if others else
                   " — aucune autre IA prête : les offres attendent et tout repart tout seul à cette heure-là"), "error")
        return
    if not pid or not _auth_error(e) or refused(pid):
        return
    msg = f"clé refusée par {label(pid)} ({str(e)[:110]})"
    if pid == "anthropic" and "bearer" in str(e).lower():
        msg += (" — une clé d'API Anthropic commence par sk-ant-api ; un jeton d'abonnement (sk-ant-oat…) ne marche pas "
                "ici : pour ton abonnement, choisis « Claude Code »")
    _REFUSED[pid] = (time.time(), msg)
    log(msg + " — fournisseur écarté 30 min (ou jusqu'à une nouvelle clé)", "error")


def refused(pid: str) -> str:
    r = _REFUSED.get(pid)
    if r and time.time() - r[0] < 1800:
        return r[1]
    _REFUSED.pop(pid, None)
    return ""


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
def _ollama_msgs(msgs: list) -> list:
    """Messages au format OpenAI -> format natif d'Ollama (arguments d'outils en objet, images à part)."""
    out = []
    for m in msgs:
        n = {"role": m["role"], "content": ""}
        c = m.get("content")
        if isinstance(c, list):
            n["content"] = "\n".join(p.get("text", "") for p in c if p.get("type") == "text")
            imgs = [p["image_url"]["url"].split(",", 1)[-1] for p in c if p.get("type") == "image_url"]
            if imgs:
                n["images"] = imgs
        else:
            n["content"] = c or ""
        if m.get("tool_calls"):
            n["tool_calls"] = []
            for tc in m["tool_calls"]:
                args = tc["function"].get("arguments") or "{}"
                n["tool_calls"].append({"function": {"name": tc["function"]["name"],
                                                     "arguments": json.loads(args) if isinstance(args, str) else args}})
        out.append(n)
    return out


_OLLAMA_LOCK = threading.Lock()
THINKERS = r"qwen3|deepseek|stageauto|magistral|r1\b|reason|think"
_NO_THINK_FLAG: set = set()


def strip_think(txt: str) -> str:
    """Retire le raisonnement d'un modèle « à réflexion » (<think>…</think>) : seule la réponse est gardée."""
    txt = re.sub(r"<think>.*?</think>", "", txt or "", flags=re.S)
    if "</think>" in txt:  # balise ouvrante fournie par le gabarit du modèle
        txt = txt.split("</think>")[-1]
    return txt.strip() if "<think>" not in txt else re.sub(r"<think>.*", "", txt, flags=re.S).strip()


def text_tool_calls(content: str, tools: list) -> list:
    """Appels d'outils qu'un modèle local a écrits en texte au lieu de les rendre structurés. Deux écritures courantes :
    <tool_call>{"name": …, "arguments": {…}}</tool_call> et <tool_call><function=nom><parameter=clé>valeur</parameter>
    </function></tool_call> (Qwen 3.5). Les valeurs sont converties selon le schéma de l'outil (booléen, entier)."""
    schemas = {t["function"]["name"]: (t["function"].get("parameters") or {}).get("properties", {}) for t in tools if t.get("function")}
    out = []
    for block in re.findall(r"<tool_call>(.*?)(?:</tool_call>|$)", strip_think(content), flags=re.S):
        m = re.search(r"<function=([\w.-]+)>(.*?)(?:</function>|$)", block, flags=re.S)
        if m:
            name, args = m.group(1), {}
            for k, v in re.findall(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", m.group(2), flags=re.S):
                args[k] = v
        else:
            d = _loads(block) or {}
            name, args = d.get("name") or "", d.get("arguments") or d.get("parameters") or {}
            if isinstance(args, str):
                args = _loads(args) or {}
        if name not in schemas:
            continue
        for k, v in list(args.items()):
            ty = (schemas[name].get(k) or {}).get("type")
            if isinstance(v, str):
                if ty == "boolean":
                    args[k] = v.strip().lower() in ("true", "1", "oui", "yes")
                elif ty == "integer" and re.fullmatch(r"-?\d+", v.strip()):
                    args[k] = int(v.strip())
                elif ty in ("object", "array"):
                    args[k] = _loads(v) if _loads(v) is not None else v
        out.append({"function": {"name": name, "arguments": args}})
    return out


def ollama_chat(payload: dict) -> dict:
    """Ollama par son API native (meilleur rendement qu'en mode compatible OpenAI) : contexte long (les consignes du
    skill dépassent les 2 048 / 4 096 jetons par défaut, qui les tronquent sans prévenir), JSON contraint par le schéma,
    température basse, modèle gardé en mémoire 30 min. Réponse renvoyée au format OpenAI."""
    s = load_settings()
    body = {"model": payload["model"], "messages": _ollama_msgs(payload.get("messages") or []), "stream": False,
            "keep_alive": "30m",
            "options": {"num_ctx": int(s.get("local_ctx", 16384)), "temperature": float(s.get("local_temperature", 0.3)),
                        "num_predict": int(min(payload.get("max_tokens") or 4096, s.get("foreign_max_tokens", 16000)))}}
    rf = payload.get("response_format") or {}
    if rf.get("type") == "json_schema":
        body["format"] = (rf.get("json_schema") or {}).get("schema") or "json"
    elif rf.get("type") == "json_object":
        body["format"] = "json"
    if payload.get("tools"):
        body["tools"] = payload["tools"]
    thinker = bool(re.search(THINKERS, payload["model"] or "", re.I)) and payload["model"] not in _NO_THINK_FLAG
    if thinker:
        body["think"] = False  # modèle « à réflexion » : réponse directe, plus rapide et JSON propre
    url = base_url("ollama").replace("/v1", "") + "/api/chat"
    tmo = httpx.Timeout(int(s.get("local_timeout", 1800)), connect=10)
    r = None
    with _OLLAMA_LOCK:  # un appel à la fois : Ollama les traite l'un après l'autre, les suivants attendent ici sans expirer
        for attempt in (1, 2, 3):
            try:
                r = httpx.post(url, json=body, timeout=tmo)
                if r.status_code == 400 and thinker and "think" in r.text.lower():
                    # ce modèle n'accepte pas le réglage « think » : on ne l'envoie plus, les balises <think> sont retirées
                    _NO_THINK_FLAG.add(payload["model"])
                    body.pop("think", None)
                    r = httpx.post(url, json=body, timeout=tmo)
                if r.status_code < 500:
                    break
            except httpx.ConnectError:
                raise ProviderError(f"{label('ollama')} : service local injoignable ({url}) — Ollama est-il lancé ?")
            except httpx.HTTPError as e:  # réponse trop longue ou coupure : nouvel essai, le modèle reste chargé
                log(f"modèle local : {e.__class__.__name__} (essai {attempt}/3)")
                r = None
            time.sleep(2 * attempt)
    if r is None:
        raise ProviderError(f"{label('ollama')} : pas de réponse du modèle local après 3 essais")
    if r.status_code >= 400:
        raise ProviderError(f"{label('ollama')} {r.status_code} : {r.text[:300]}")
    msg = r.json().get("message") or {}
    raw_calls = msg.get("tool_calls") or []
    if not raw_calls and payload.get("tools"):  # appel d'outil écrit en texte (gabarit du modèle non reconnu par Ollama)
        raw_calls = text_tool_calls(msg.get("content") or "", payload["tools"])
        if raw_calls:
            msg["content"] = ""
    calls = [{"id": f"call_{i}", "type": "function",
              "function": {"name": tc["function"]["name"], "arguments": json.dumps(tc["function"].get("arguments") or {})}}
             for i, tc in enumerate(raw_calls)]
    m = {"role": "assistant", "content": strip_think(msg.get("content") or "")}
    if calls:
        m["tool_calls"] = calls
    j = r.json()
    return {"choices": [{"message": m, "finish_reason": "tool_calls" if calls else "stop"}],
            "_stats": {k: j.get(k) or 0 for k in ("prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration",
                                                    "load_duration")}}


def ollama_gpu(model: str) -> str:
    """Où tourne le modèle chargé : part de la carte graphique d'après Ollama (/api/ps)."""
    try:
        r = httpx.get(base_url("ollama").replace("/v1", "") + "/api/ps", timeout=3).json()
        for m in r.get("models", []):
            if m.get("name") == model or m.get("model") == model:
                size, vram = m.get("size") or 0, m.get("size_vram") or 0
                if not size:
                    return ""
                pct = round(100 * vram / size)
                return "100 % carte graphique" if pct >= 99 else ("processeur seulement" if pct <= 1 else
                                                                  f"{pct} % carte graphique, le reste sur le processeur")
    except Exception:
        pass
    return ""


def ollama_estimate() -> dict:
    """Mesure la vitesse du modèle local sur une demande de la taille d'une étape réelle et en déduit le temps d'une
    candidature : CV + lettre (adaptation courte, compilation) puis formulaire (~8 étapes). La vitesse d'écriture est
    mesurée au chronomètre (jetons écrits / temps réel hors lecture), sur une demande différente à chaque fois : les
    durées annoncées par Ollama sont fausses quand la demande est déjà en mémoire ou que la réponse est très courte."""
    model = model_of("ollama")
    filler = ("Profil : étudiant en Master 2 Sécurité et Réseaux, stage de fin d'études de 6 mois, cybersécurité, SOC, "
              "réseaux, DevOps. ") * 60  # ~1 500 jetons, comme une étape de formulaire

    def ask(n):
        payload = {"model": model, "max_tokens": 220,
                   "messages": [{"role": "system", "content": "Réponds en français."},
                                {"role": "user", "content": f"Demande n° {time.time():.0f}-{n}. " + filler +
                                 "\nÉcris un paragraphe de motivation de 120 mots, sans titre."}]}
        t0 = time.time()
        st = ollama_chat(payload)["_stats"]
        return st, time.time() - t0
    ask(0)  # chargement du modèle en mémoire (non compté)
    st, wall = ask(1)
    n_out, n_in = st["eval_count"], st["prompt_eval_count"]
    read_s = st["prompt_eval_duration"] / 1e9
    if not n_out:
        return {}
    gen_wall = n_out / max(wall - read_s, 0.05)                       # au chronomètre
    gen_api = n_out / max(st["eval_duration"] / 1e9, 1e-3) if st["eval_duration"] else gen_wall
    gen = max(gen_wall, gen_api) if n_out < 40 else gen_wall          # réponse très courte : mesure peu fiable
    pp = n_in / max(read_s, 1e-3) if n_in and read_s > 0.05 else gen * 8
    cv = 3000 / pp + 700 / gen + 15          # adaptation courte (lecture de l'offre, réponse) + compilation LaTeX
    form = 8 * (1500 / pp + 120 / gen + 3)  # 8 étapes : page lue, action décidée, action faite dans Chrome
    return {"lecture_tok_s": round(pp), "ecriture_tok_s": round(gen, 1), "cv_s": round(cv), "formulaire_s": round(form),
            "total_min": round((cv + form) / 60, 1), "modele": model, "gpu": ollama_gpu(model),
            "mesure": f"{n_out} jetons écrits en {wall:.1f} s", "fiable": n_out >= 40}


def oa_post(payload: dict, pid: str) -> dict:
    if pid == "ollama" and load_settings().get("ollama_native", True):
        return ollama_chat(payload)
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
    exe = claude_bin(deep=True)
    if not exe:
        raise ProviderError("Claude Code introuvable : " + INSTALL_HINT)
    CC_DIR.mkdir(parents=True, exist_ok=True)
    wsl = exe == "wsl"
    base = ["wsl.exe", "-e", "bash", "-lc", _WSL_FIND, "claude"] if wsl else [exe]
    # consigne en ASCII : rien à convertir entre pages de code (claude.cmd passe par l'invite de commandes)
    args = base + ["-p", "Read the full request given on standard input and answer exactly in the requested format.",
            "--output-format", "json",
            "--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,Read,Glob,Grep"]
    if system:
        import hashlib
        f = CC_DIR / f"consignes_{hashlib.sha1(system.encode('utf-8')).hexdigest()[:12]}.txt"
        if not f.exists():
            f.write_text(system, encoding="utf-8")
        args += ["--system-prompt-file", _wsl_path(f) if wsl else str(f)]
    if model_of("claude_code"):
        args += ["--model", model_of("claude_code")]
    if resume:
        args += ["--resume", resume]
    try:
        r = subprocess.run(args, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=str(CC_DIR), env=_cc_env(), creationflags=0x08000000 if IS_WIN else 0)
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
        msg = str(((j or {}).get("result") if isinstance(j, dict) else None) or r.stderr or out or "erreur inconnue")
        if re.search(r"log ?in|/login|api key|auth|credential|not logged", msg, re.I):
            msg += _login_hint(exe)
        if re.search(r"git.?bash", msg, re.I):
            msg += " — Claude Code sous Windows demande Git pour Windows (git-scm.com) : installe-le puis réessaie."
        raise ProviderError(f"Claude Code : {msg[-400:]}")
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
            note_failure(self.provider, e)
            if len(self.users) != 1 or not _fallback_worthy(e):
                raise
            for nxt in self.order[self.order.index(self.provider) + 1:]:
                if refused(nxt):
                    continue
                log(f"{label(self.provider)} indisponible ({str(e)[:80]}) : relais par {label(nxt)}")
                self.provider, self.msgs, self.cc_session = nxt, [{"role": "user", "content": text}], None
                try:
                    return self._ask()
                except Exception as e2:
                    note_failure(nxt, e2)
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
            note_failure(pid, e)
            log(f"{label(pid)} : {e.__class__.__name__} {str(e)[:100]}")
    return None


def test(pid: str) -> dict:
    """Bouton « Tester » : une petite question à ce fournisseur (même s'il avait été écarté : on réessaie)."""
    _REFUSED.pop(pid, None)
    _LIMITED.pop(pid, None)
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
        out = {"ok": True, "answer": d, "seconds": round(time.time() - t0, 1)}
        if pid == "ollama" and load_settings().get("ollama_native", True):
            try:
                out["estimate"] = ollama_estimate()
            except Exception as e:
                log(f"estimation Ollama : {e.__class__.__name__}")
        return out
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


VIEW_MARK = "\nÉléments ("  # début de la liste d'éléments d'une vue de page (agent.render)


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
            note_failure(self.provider, e)
            if self.turns != 0 or not _fallback_worthy(e):
                raise
            st = None
            for nxt in self.order[self.order.index(self.provider) + 1:]:
                if refused(nxt):
                    continue
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

    def _compact(self):
        """Économie de jetons : les anciennes vues de page (liste d'éléments, captures) sont remplacées par une ligne.
        Seule la dernière vue sert : les références des précédentes ne sont plus valides. Sans cela, chaque étape
        renvoie à l'IA toutes les pages déjà lues."""
        def short(c, head=600):
            if isinstance(c, list):
                return "(ancienne capture effacée)"
            if isinstance(c, str) and VIEW_MARK in c:
                return c.split(VIEW_MARK)[0][:head].rstrip() + "\n(ancienne vue de page effacée : références périmées, relis la page si besoin)"
            return c
        for m in self.msgs:
            c = m.get("content")
            if m.get("role") == "tool":
                m["content"] = short(c)
            elif m.get("role") == "user" and isinstance(c, list):
                if any(isinstance(b, dict) and b.get("type") == "image_url" for b in c):
                    m["content"] = "(ancienne capture effacée)"
                    continue
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        b["content"] = short(b.get("content"))
            elif m.get("role") == "user" and isinstance(c, str):
                m["content"] = short(c, 20000)  # consigne de départ gardée en entier, sans la première vue de page

    def results(self, results: list):
        """results : [{id, content (str ou liste avec image), is_error}]"""
        kind = PROVIDERS[self.provider]["kind"] if self.provider else "anthropic"
        if kind != "claude_code" and any(isinstance(r["content"], list) or VIEW_MARK in str(r["content"]) for r in results):
            self._compact()
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
