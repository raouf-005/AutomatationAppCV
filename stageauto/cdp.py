"""Couche navigateur commune, pilotée par le protocole Chrome DevTools (CDP).

Deux façons d'atteindre Chrome, même interface (Tab) :
- « extension » (par défaut) : l'extension StageAuto Bridge, chargée dans TON Chrome habituel, relaie les commandes
  CDP via chrome.debugger (même principe que Claude in Chrome) : tes sessions déjà ouvertes sont utilisées telles quelles ;
- « playwright » : Chrome lancé par l'application avec un profil dédié (secours / tests).

Les actions sont « réelles » (clics souris et saisie clavier de confiance), pas des clics JavaScript.
"""
import asyncio
import base64
import itertools
import json
import mimetypes
import os
import random
import re
import subprocess
import time
from pathlib import Path

from .config import CHROME_PROFILE, IS_WIN, load_settings
from .notify import log


class BrowserError(Exception):
    pass


# ----------------------------------------------------------------------------------------------- JS
SNAP_JS = r"""
(opts) => {
  opts = opts || {};
  const MAX = opts.max || 220;
  // keep : lecture de contrôle qui ne renumérote pas les références en cours d'utilisation
  const reg = opts.keep ? {map: {}, n: 0} : (window.__sa = {map: {}, n: 0});
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const cut = (t, n) => { t = clean(t); return t.length > n ? t.slice(0, n - 1) + '…' : t; };
  const visible = el => { const r = el.getBoundingClientRect(); if (r.width < 1 || r.height < 1) return false;
    const s = el.ownerDocument.defaultView.getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05; };
  const labelOf = el => {
    let t = '';
    const own = l => [...l.childNodes].filter(n => n.nodeType === 3 || (n.nodeType === 1 && !n.contains(el) &&
        !['SELECT', 'INPUT', 'TEXTAREA', 'OPTION'].includes(n.tagName))).map(n => n.textContent).join(' ');
    if (el.labels && el.labels.length) t = [...el.labels].map(l => l.contains(el) ? own(l) : l.innerText).join(' ');
    const lb = el.getAttribute('aria-labelledby');
    if (!t && lb) t = lb.split(' ').map(id => (el.ownerDocument.getElementById(id) || {}).innerText || '').join(' ');
    if (!t) t = el.getAttribute('aria-label') || '';
    if (!t && ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) {
      let p = el.parentElement;
      for (let i = 0; i < 4 && p && !t; i++, p = p.parentElement) {
        const c = p.querySelector('label, legend, [class*=label], [class*=Label], [class*=question]');
        if (c && !c.contains(el) && clean(c.innerText).length > 1) t = c.innerText;
      }
    }
    return clean(t);
  };
  // titre de la partie du formulaire où se trouve le champ (dernier titre qui le précède : « Formation »,
  // « Expérience professionnelle »…) et légende de son groupe : une « date de début » n'a pas le même sens
  // dans la formation que pour la disponibilité
  let HEADS = null;
  const sectionOf = el => {
    if (!HEADS) HEADS = [...document.querySelectorAll('h1, h2, h3, h4, h5, [role=heading]')].filter(visible);
    let last = null;
    for (const h of HEADS) {
      if (h.contains(el)) continue;
      if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) last = h; else break; }
    const out = [];
    const fs = el.closest('fieldset'), lg = fs && fs.querySelector(':scope > legend');
    if (lg) out.push(cut(lg.innerText, 40));
    if (last) out.push(cut(last.innerText, 50));
    return out.filter(x => x).join(' / '); };
  const groupOf = el => { const fs = el.closest('fieldset, [role=radiogroup], [role=group]');
    if (fs) { const lg = fs.querySelector('legend, [class*=label], [class*=question], span, p'); if (lg) return cut(lg.innerText, 140); }
    return el.name || ''; };
  const roleOf = el => {
    const r = el.getAttribute('role'); if (r) return r;
    const t = el.tagName;
    if (t === 'A') return 'link'; if (t === 'BUTTON' || t === 'SUMMARY') return 'button';
    if (t === 'SELECT') return 'select'; if (t === 'TEXTAREA') return 'textarea';
    if (t === 'INPUT') { const ty = (el.type || 'text').toLowerCase();
      if (['button', 'submit', 'reset', 'image'].includes(ty)) return 'button';
      if (ty === 'checkbox' || ty === 'radio' || ty === 'file') return ty; return 'textbox:' + ty; }
    if (el.isContentEditable) return 'textbox:editable';
    return t.toLowerCase(); };
  // portée : dialogue modal visible le plus haut, sinon la page
  let roots = [document];
  const dialogs = [...document.querySelectorAll('[role=dialog], [aria-modal=true], dialog[open]')].filter(visible);
  const inDialog = dialogs.length > 0 && !opts.page;
  if (inDialog) roots = [dialogs[dialogs.length - 1]];
  const SEL = 'a[href], button, input, select, textarea, summary, [role=button], [role=link], [role=checkbox], [role=radio], ' +
    '[role=combobox], [role=option], [role=tab], [role=menuitem], [role=menuitemradio], [role=switch], [role=textbox], ' +
    '[role=listbox], [contenteditable=true], [contenteditable=""]';
  const items = []; const seen = new Set();
  const walk = (root, depth) => {
    if (depth > 6) return;
    root.querySelectorAll(SEL).forEach(el => {
      if (items.length >= MAX || seen.has(el)) return; seen.add(el);
      const isFile = el.tagName === 'INPUT' && el.type === 'file';
      const isToggle = el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio');
      if (el.disabled && !isToggle) return;
      if (el.type === 'hidden') return;
      if (!isFile && !visible(el)) { if (!(isToggle && el.labels && el.labels[0] && visible(el.labels[0]))) return; }
      const ref = 'e' + (reg.n++); reg.map[ref] = el;
      const role = roleOf(el);
      let name = labelOf(el);
      if (!name || role === 'link' || role === 'button' || role === 'option' || role === 'tab' || role.startsWith('menuitem'))
        name = name || el.innerText || el.value || el.getAttribute('title') || el.getAttribute('alt') || '';
      if (!name) name = el.placeholder || el.getAttribute('title') || el.name || el.id || '';
      const it = {ref, role, name: cut(name, 110)};
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable) {
        if (isToggle) { it.checked = el.checked; if (el.type === 'radio') it.group = groupOf(el); }
        else if (isFile) { it.accept = el.accept || ''; it.files = el.files ? el.files.length : 0; }
        else { it.value = cut(el.isContentEditable ? el.innerText : el.value, 80);
          const sec = sectionOf(el); if (sec) it.section = sec; }
        if (el.placeholder) it.placeholder = cut(el.placeholder, 50);
      }
      if (el.getAttribute('aria-checked')) it.checked = el.getAttribute('aria-checked') === 'true';
      if (el.getAttribute('aria-expanded')) it.expanded = el.getAttribute('aria-expanded') === 'true';
      if (el.tagName === 'SELECT') { it.value = cut((el.options[el.selectedIndex] || {}).text, 60);
        it.options = [...el.options].map(o => cut(o.text, 50)).filter(x => x).slice(0, 25); }
      if (el.required || el.getAttribute('aria-required') === 'true') it.required = true;
      if (el.getAttribute('aria-invalid') === 'true') it.invalid = true;
      if (el.tagName === 'A') it.href = (el.getAttribute('href') || '').slice(0, 120);
      items.push(it);
    });
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot, depth + 1); });
    root.querySelectorAll('iframe').forEach(f => { try { if (f.contentDocument && visible(f)) walk(f.contentDocument, depth + 1); } catch (e) {} });
  };
  roots.forEach(r => walk(r, 0));
  const scope = inDialog ? roots[0] : document.body;
  const text = cut(scope ? scope.innerText : '', opts.textMax || 2500);
  const errs = [...(inDialog ? roots[0] : document).querySelectorAll('[role=alert], [aria-live=assertive], .error, [class*=error-message], [class*=errorMessage], .invalid-feedback, [class*=feedback--error]')]
     .filter(visible).map(e => cut(e.innerText, 140)).filter(x => x).slice(0, 6);
  const captcha = [...document.querySelectorAll("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], iframe[src*='challenges.cloudflare'], #challenge-form, .g-recaptcha, .h-captcha")].some(visible)
     || /just a moment|un instant|attention required/i.test(document.title);
  return {url: location.href, title: document.title, dialog: inDialog, items, text, errors: errs, captcha,
          truncated: items.length >= MAX};
}
"""

RECT_JS = r"""
(ref) => {
  const reg = window.__sa; if (!reg || !reg.map[ref]) return {error: 'ref inconnue : relis la page (read_page)'};
  let el = reg.map[ref];
  if (!el.isConnected) return {error: 'élément disparu : relis la page (read_page)'};
  let target = el;
  const r0 = el.getBoundingClientRect();
  if ((r0.width < 2 || r0.height < 2) && el.labels && el.labels[0]) target = el.labels[0];
  target.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const r = target.getBoundingClientRect();
  let x = r.left + r.width / 2, y = r.top + r.height / 2;
  let w = target.ownerDocument.defaultView;
  while (w.frameElement) { const fr = w.frameElement.getBoundingClientRect(); x += fr.left; y += fr.top; w = w.parent; }
  const name = (el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) || el.innerText ||
    el.placeholder || el.value || el.name || '').replace(/\s+/g, ' ').trim().slice(0, 40);
  return {x, y, w: r.width, h: r.height, tag: el.tagName, type: el.type || '', checked: !!el.checked, name};
}
"""

FOCUS_JS = r"""
(ref) => { const el = (window.__sa || {map: {}}).map[ref]; if (!el) return {error: 'ref inconnue'};
  el.scrollIntoView({block: 'center'}); el.focus();
  if (el.select && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) { try { el.select(); } catch (e) {} }
  else if (el.isContentEditable) { const s = el.ownerDocument.getSelection(); s.selectAllChildren(el); }
  return {tag: el.tagName, type: el.type || '', editable: el.isContentEditable}; }
"""

VALUE_JS = r"""
(a) => { const el = (window.__sa || {map: {}}).map[a.ref]; if (!el) return null;
  if (a.set !== undefined) {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const d = Object.getOwnPropertyDescriptor(proto, 'value'); d.set.call(el, a.set);
    el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
  }
  return el.isContentEditable ? el.innerText : el.value; }
"""

SELECT_JS = r"""
(a) => { const el = (window.__sa || {map: {}}).map[a.ref]; if (!el) return {error: 'ref inconnue'};
  if (el.tagName !== 'SELECT') return {error: "ce n'est pas une liste <select> : clique dessus puis clique l'option"};
  const n = s => (s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();
  const opts = [...el.options]; const w = n(a.value);
  let o = opts.find(o => n(o.text) === w) || opts.find(o => n(o.value) === w) || opts.find(o => n(o.text).startsWith(w))
       || opts.find(o => w && n(o.text).includes(w));
  if (!o) return {error: 'option introuvable', options: opts.map(o => o.text).slice(0, 30)};
  el.value = o.value; o.selected = true;
  el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
  return {selected: o.text}; }
"""

FILE_INPUT_JS = r"""
(ref) => { const el = (window.__sa || {map: {}}).map[ref]; if (!el) return null;
  if (el.tagName === 'INPUT' && el.type === 'file') return el;
  const scopes = [el.closest('form'), el.closest('[role=dialog]'), el.parentElement && el.parentElement.parentElement, el.ownerDocument];
  for (const s of scopes) { if (!s) continue;
    const c = [...s.querySelectorAll('input[type=file]')].filter(i => !/image/.test(i.accept || '') || /pdf/.test(i.accept || ''));
    if (c.length) return c[c.length - 1]; }
  return null; }
"""

KEYS = {"Enter": (13, "\r"), "Tab": (9, ""), "Escape": (27, ""), "Backspace": (8, ""), "ArrowDown": (40, ""),
        "ArrowUp": (38, ""), "ArrowLeft": (37, ""), "ArrowRight": (39, ""), "Space": (32, " "), "Delete": (46, "")}


# ------------------------------------------------------------------------------------------- Tab
CURSOR_JS = r"""
(p) => {
  if (window.top !== window) return;  // un seul curseur, dans la page principale
  const draw = () => {
    const d = document; let c = d.getElementById('__sa_cursor');
    if (!c) {
      c = d.createElement('div'); c.id = '__sa_cursor';
      c.style.cssText = 'position:fixed;left:0;top:0;width:22px;height:22px;z-index:2147483647;pointer-events:none;will-change:transform';
      c.innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22" style="filter:drop-shadow(0 1px 2px rgba(0,0,0,.35))">' +
        '<path d="M2 2 L2 18 L7 13.5 L10.5 20.5 L13.5 19 L10 12 L17 12 Z" fill="#1f7a4d" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>' +
        '<span style="position:absolute;left:20px;top:17px;white-space:nowrap;max-width:320px;overflow:hidden;text-overflow:ellipsis;' +
        'font:600 12px/1.25 system-ui,Segoe UI,sans-serif;color:#fff;background:#1f7a4d;border-radius:6px;padding:3px 8px;' +
        'box-shadow:0 2px 8px rgba(0,0,0,.25);opacity:0;transition:opacity .2s"></span>';
      (d.body || d.documentElement).appendChild(c);
    }
    c.style.transition = p.ms ? `transform ${p.ms}ms cubic-bezier(.25,.1,.25,1)` : 'none';
    c.style.transform = `translate(${p.x - 2}px, ${p.y - 2}px)`;
    const l = c.querySelector('span');
    if (l && p.label !== undefined && p.label !== null) {
      l.textContent = p.label; l.style.opacity = p.label ? '1' : '0';
      clearTimeout(window.__saLabelT);
      if (p.label) window.__saLabelT = setTimeout(() => { l.style.opacity = '0'; }, 2600);
    }
    if (p.click) {
      const r = d.createElement('div');
      r.style.cssText = `position:fixed;left:${p.x - 12}px;top:${p.y - 12}px;width:24px;height:24px;border-radius:50%;border:2px solid #1f7a4d;z-index:2147483646;pointer-events:none;transition:transform .45s,opacity .45s`;
      d.documentElement.appendChild(r);
      requestAnimationFrame(() => { r.style.transform = 'scale(1.9)'; r.style.opacity = '0'; });
      setTimeout(() => r.remove(), 500);
    }
    try { sessionStorage.setItem('__sa_pos', JSON.stringify([p.x, p.y])); } catch (e) {}
  };
  if (p.boot) {  // nouvelle page : le curseur réapparaît là où il était
    try { const q = JSON.parse(sessionStorage.getItem('__sa_pos') || 'null'); if (q) { p.x = q[0]; p.y = q[1]; } } catch (e) {}
    if (p.x === undefined) { p.x = innerWidth * 0.35; p.y = innerHeight * 0.3; }
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', draw); return; }
  }
  draw();
}
"""

# Parcours visible d'une page (mode spectateur) : le curseur passe sur les offres ou les lignes lues
TOUR_JS = r"""(a) => { const els = [...document.querySelectorAll(a.sel)].filter(e => { const r = e.getBoundingClientRect();
    return r.width > 30 && r.height > 8 && getComputedStyle(e).visibility !== 'hidden'; });
  const step = Math.max(1, Math.floor(els.length / a.max)); const pick = [];
  for (let i = 0; i < els.length && pick.length < a.max; i += step) pick.push(els[i]);
  window.__saTour = pick; return pick.length; }"""
TOUR_STEP_JS = r"""(i) => { const e = (window.__saTour || [])[i]; if (!e || !e.isConnected) return null;
  e.scrollIntoView({block: 'center', behavior: 'smooth'}); return true; }"""
TOUR_RECT_JS = r"""(i) => { const e = (window.__saTour || [])[i]; if (!e) return null; const r = e.getBoundingClientRect();
  return {x: r.left + Math.min(r.width * 0.25, 140), y: r.top + Math.min(r.height / 2, 24),
          t: (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 38)}; }"""


def _bezier(p0, p3, n):
    """Trajectoire courbe et naturelle entre deux points (courbe de Bézier cubique, vitesse en cloche)."""
    (x0, y0), (x3, y3) = p0, p3
    dx, dy = x3 - x0, y3 - y0
    dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
    nx, ny = -dy / dist, dx / dist
    b1, b2 = random.uniform(-0.25, 0.25) * dist, random.uniform(-0.25, 0.25) * dist
    x1, y1 = x0 + dx * 0.3 + nx * b1, y0 + dy * 0.3 + ny * b1
    x2, y2 = x0 + dx * 0.7 + nx * b2, y0 + dy * 0.7 + ny * b2
    pts = []
    for i in range(1, n + 1):
        t = i / n
        t = t * t * (3 - 2 * t)  # accélération puis freinage
        u = 1 - t
        pts.append((u ** 3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
                    u ** 3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3))
    return pts


class Tab:
    def __init__(self, browser, tid):
        self.browser = browser
        self.id = tid
        self.last_url = ""
        self.pos = (random.uniform(200, 600), random.uniform(150, 400))
        self._boot = False

    def human(self) -> bool:
        """Souris et clavier « humains » : sur LinkedIn par défaut (réglage human_mode : linkedin / all / off)."""
        mode = load_settings().get("human_mode", "linkedin")
        return mode == "all" or (mode == "linkedin" and "linkedin.com" in (self.last_url or ""))

    @staticmethod
    def spectator() -> bool:
        """Mode spectateur : le curseur glisse, parcourt les pages et annonce ce qu'il fait (un peu plus lent)."""
        st = load_settings()
        return bool(st.get("show_cursor", True) and st.get("spectator_mode", True))

    async def _cursor(self, x, y, click=False, ms=0, label=None):
        if load_settings().get("show_cursor", True):
            arg = {"x": x, "y": y, "click": click, "ms": ms}
            if label is not None and self.spectator():
                arg["label"] = label
            try:
                await self.eval(CURSOR_JS, arg, timeout=5)
            except Exception:
                pass

    async def _ensure_boot(self):
        """Le curseur est redessiné automatiquement sur chaque nouvelle page de cet onglet."""
        if self._boot or not load_settings().get("show_cursor", True):
            return
        self._boot = True
        try:
            await self.send("Page.addScriptToEvaluateOnNewDocument", {"source": "(" + CURSOR_JS + ')({"boot": true})'})
        except Exception:
            pass

    async def tour(self, selector: str, label="Lecture", max_points=5):
        """Mode spectateur : le curseur parcourt quelques éléments de la page (offres, lignes de l'annonce) en la faisant défiler."""
        if not self.spectator():
            return
        try:
            n = await self.eval(TOUR_JS, {"sel": selector, "max": max_points}, timeout=5) or 0
            for i in range(n):
                if not await self.eval(TOUR_STEP_JS, i, timeout=5):
                    continue
                await asyncio.sleep(0.3)
                r = await self.eval(TOUR_RECT_JS, i, timeout=5)
                if r:
                    await self.move(r["x"], r["y"], label=f"{label} : {r['t']}" if r.get("t") else label)
                await asyncio.sleep(0.15)
        except Exception:
            pass

    async def send(self, method, params=None):
        return await self.browser.cdp(self.id, method, params or {})

    async def eval(self, fn: str, arg=None, timeout=30):
        is_fn = re.match(r"^\s*(async\s+)?(\([^)]*\)|\w+)\s*=>|^\s*(async\s+)?function\b", fn)
        expr = f"({fn})({json.dumps(arg, ensure_ascii=False)})" if is_fn else fn
        r = await asyncio.wait_for(self.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True, "userGesture": True}), timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise BrowserError((d.get("exception") or {}).get("description") or d.get("text") or "erreur JS")
        return (r.get("result") or {}).get("value")

    async def wait_ready(self, timeout=25):
        t0 = time.time()
        await asyncio.sleep(0.15)
        while time.time() - t0 < timeout:
            try:
                st = await self.eval("document.readyState", timeout=5)
                if st in ("interactive", "complete"):
                    await asyncio.sleep(0.25)
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    async def settle(self, max_wait=8.0, min_wait=0.3):
        """Attend que la page soit chargée et que le DOM ne bouge plus (au lieu d'une pause fixe)."""
        t0 = time.time()
        await asyncio.sleep(min_wait)
        last = None
        while time.time() - t0 < max_wait:
            try:
                st = await self.eval("() => [document.readyState, document.getElementsByTagName('*').length,"
                                     " !!document.querySelector('[aria-busy=true]')]", timeout=5)
            except Exception:
                st = None
            if st and st[0] == "complete" and not st[2] and last is not None and abs(st[1] - last) < 3:
                return True
            last = st[1] if st else None
            await asyncio.sleep(0.25)
        return False

    async def wait_for(self, js_predicate: str, timeout=10.0, every=0.25):
        """Attend qu'une condition JS soit vraie (ex. cartes d'offres affichées). Renvoie sa valeur ou None."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                v = await self.eval(js_predicate, timeout=5)
                if v:
                    return v
            except Exception:
                pass
            await asyncio.sleep(every)
        return None

    async def goto(self, url, timeout=40):
        self.last_url = url
        await self._ensure_boot()
        r = await self.send("Page.navigate", {"url": url})
        if r.get("errorText"):
            log(f"navigation : {r['errorText']} ({url[:70]})")
        await self.wait_ready(timeout)
        await self.refuse_cookies()
        host = re.sub(r"^https?://(www\.)?([^/]+).*", r"\2", url)
        await self._cursor(*self.pos, label=f"Ouverture : {host[:40]}")

    async def url(self):
        try:
            return await self.eval("location.href", timeout=8)
        except Exception:
            return ""

    async def cookies(self, urls):
        """Cookies du vrai Chrome pour ces adresses (y compris HttpOnly, comme la session LinkedIn)."""
        try:
            r = await self.send("Network.getCookies", {"urls": urls})
            return r.get("cookies", []) or []
        except Exception:
            return []

    async def text(self, sel="body", limit=15000):
        return (await self.eval("(s) => { const e = document.querySelector(s); return e ? e.innerText : '' }", sel) or "")[:limit]

    async def snapshot(self, **opts):
        snap = await self.eval(SNAP_JS, opts)
        if snap and snap.get("url"):
            self.last_url = snap["url"]
        return snap

    async def move(self, x, y, label=None):
        """Déplacement de la souris : trajectoire courbe à vitesse variable en mode humain ; glissement fluide du curseur
        en mode spectateur ; saut direct sinon (le plus rapide)."""
        dist = ((x - self.pos[0]) ** 2 + (y - self.pos[1]) ** 2) ** 0.5
        if self.human():
            n = int(min(38, max(10, dist / 22)))
            total = min(0.95, 0.22 + dist / 1600) * random.uniform(0.85, 1.2)
            for i, (px, py) in enumerate(_bezier(self.pos, (x, y), n)):
                await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": px, "y": py})
                if i % 3 == 0 or i == n - 1:
                    await self._cursor(px, py, ms=int(total / n * 3000), label=label if i == 0 else None)
                await asyncio.sleep(total / n)
        elif self.spectator():
            ms = int(min(480, 140 + dist * 0.35))
            await self._cursor(x, y, ms=ms, label=label)
            await asyncio.sleep(ms / 1000)
            await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        else:
            await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            await self._cursor(x, y)
        self.pos = (x, y)

    async def _goto_el(self, ref, label):
        """Le curseur va sur l'élément avant d'agir (visible en mode spectateur et en mode humain)."""
        if not (self.spectator() or self.human()):
            return None
        r = await self.eval(RECT_JS, ref)
        if r and not r.get("error"):
            await self.move(r["x"], r["y"], label=label.format(name=r.get("name") or ""))
        return r

    async def _mouse(self, x, y, clicks=1, label=None):
        human = self.human()
        await self.move(x, y, label=label)
        await asyncio.sleep(random.uniform(0.08, 0.22) if human else 0.05)
        await self.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": clicks})
        await asyncio.sleep(random.uniform(0.05, 0.13) if human else 0.04)
        await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": clicks})
        await self._cursor(x, y, click=True)

    async def click(self, ref):
        r = await self.eval(RECT_JS, ref)
        if not r or r.get("error"):
            raise BrowserError((r or {}).get("error", "élément introuvable"))
        if r["type"] == "file":
            raise BrowserError("champ fichier : utilise upload")
        x, y = r["x"], r["y"]
        if self.human():  # point de clic naturel dans la zone centrale de l'élément, pas au pixel près
            x += random.uniform(-0.22, 0.22) * min(r["w"], 160)
            y += random.uniform(-0.2, 0.2) * min(r["h"], 40)
        await asyncio.sleep(0.15)
        await self._mouse(x, y, label=f"Clic : {r.get('name')}" if r.get("name") else "Clic")
        return r

    async def _type_human(self, text):
        """Frappe caractère par caractère, rythme irrégulier ; les textes longs sont saisis par petits blocs."""
        if len(text) > 700:
            for i in range(0, len(text), 40):
                await self.send("Input.insertText", {"text": text[i:i + 40]})
                await asyncio.sleep(random.uniform(0.05, 0.15))
            return
        for ch in text:
            if ch == "\n":  # saut de ligne inséré sans touche Entrée (qui enverrait un message LinkedIn)
                await self.send("Input.insertText", {"text": "\n"})
            else:
                await self.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "key": ch, "unmodifiedText": ch})
                await self.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch})
            d = random.uniform(0.035, 0.11)
            if ch in " ,.;:" and random.random() < 0.25:
                d += random.uniform(0.1, 0.35)
            await asyncio.sleep(d)

    async def type(self, ref, text, submit=False):
        if self.human():  # clic dans le champ avant de taper, comme une personne
            try:
                await self.click(ref)
            except BrowserError:
                pass
        else:
            await self._goto_el(ref, "Saisie : {name}")
        f = await self.eval(FOCUS_JS, ref)
        if not f or f.get("error"):
            raise BrowserError((f or {}).get("error", "élément introuvable"))
        if text and self.human():
            await self._type_human(text)
        elif text:
            await self.send("Input.insertText", {"text": text})
        else:
            await self.press("Backspace")
        await asyncio.sleep(0.15)
        got = await self.eval(VALUE_JS, {"ref": ref})
        if f.get("tag") in ("INPUT", "TEXTAREA") and got is not None and (got or "").strip() != text.strip() \
                and f.get("type") not in ("file",):
            await self.eval(VALUE_JS, {"ref": ref, "set": text})
        if submit:
            await self.press("Enter")
        return got

    async def select(self, ref, value):
        await self._goto_el(ref, f"Choix : {str(value)[:30]}")
        return await self.eval(SELECT_JS, {"ref": ref, "value": value})

    async def check(self, ref, checked=True):
        r = await self.eval(RECT_JS, ref)
        if r and not r.get("error") and bool(r.get("checked")) != bool(checked):
            await self._mouse(r["x"], r["y"], label=f"{'Coche' if checked else 'Décoche'} : {r.get('name') or ''}")
            await asyncio.sleep(0.2)
        return True

    async def upload(self, ref, path: str):
        p = Path(path)
        if not p.exists():
            raise BrowserError(f"fichier absent : {p.name}")
        await self._goto_el(ref, f"Fichier : {p.name[:34]}")
        last = None
        for attempt in range(3):  # une reconnexion de l'extension invalide les références d'objets : on recommence
            r = await self.send("Runtime.evaluate", {"expression": f"({FILE_INPUT_JS})({json.dumps(ref)})", "returnByValue": False})
            obj = (r.get("result") or {}).get("objectId")
            if not obj:
                raise BrowserError("aucun champ de fichier près de cet élément")
            try:
                await self.send("DOM.setFileInputFiles", {"files": [str(p.resolve())], "objectId": obj})
                break
            except Exception as e:
                last = e
                if attempt < 2 and re.search(r"object|context|Detached|not attached|different extension", str(e), re.I):
                    await asyncio.sleep(0.3)
                    continue
                try:  # repli : DataTransfer (fichier injecté par JS)
                    log(f"setFileInputFiles refusé ({str(e)[:80]}), repli DataTransfer")
                    b64 = base64.b64encode(p.read_bytes()).decode()
                    mime = mimetypes.guess_type(p.name)[0] or "application/pdf"
                    await self.send("Runtime.callFunctionOn", {"objectId": obj, "awaitPromise": True, "functionDeclaration":
                        "function(b64, name, mime){ const bin = atob(b64); const u = new Uint8Array(bin.length);"
                        " for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);"
                        " const dt = new DataTransfer(); dt.items.add(new File([u], name, {type: mime})); this.files = dt.files;"
                        " this.dispatchEvent(new Event('input', {bubbles: true})); this.dispatchEvent(new Event('change', {bubbles: true})); }",
                        "arguments": [{"value": b64}, {"value": p.name}, {"value": mime}]})
                    break
                except Exception as e2:
                    last = e2
                    if attempt >= 2:
                        raise BrowserError(f"envoi du fichier impossible : {str(last)[:120]}")
        await asyncio.sleep(1.0)
        return True

    async def press(self, key):
        code, txt = KEYS.get(key, (0, key if len(key) == 1 else ""))
        base = {"windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code, "key": key if key != "Space" else " ",
                "code": key if key not in ("Space",) else "Space"}
        await self.send("Input.dispatchKeyEvent", {"type": "keyDown", **base, **({"text": txt} if txt else {})})
        await self.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    async def scroll(self, dy=600):
        if self.human():  # molette, par crans
            vp = await self.eval("({w: innerWidth, h: innerHeight})")
            x, y = vp["w"] * random.uniform(0.4, 0.6), vp["h"] * random.uniform(0.4, 0.6)
            await self.move(x, y)
            for _ in range(max(1, abs(dy) // 120)):
                await self.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0,
                                                             "deltaY": 120 if dy > 0 else -120})
                await asyncio.sleep(random.uniform(0.06, 0.16))
            return
        smooth = "smooth" if self.spectator() else "instant"
        await self.eval("(a) => { const d = [...document.querySelectorAll('[role=dialog], [aria-modal=true]')].pop();"
                        " const s = d ? ([...d.querySelectorAll('*')].find(e => e.scrollHeight > e.clientHeight + 20 &&"
                        " /auto|scroll/.test(getComputedStyle(e).overflowY)) || d) : null;"
                        " (s || window).scrollBy({top: a.dy, behavior: a.b}); }", {"dy": dy, "b": smooth})
        if smooth == "smooth":
            await asyncio.sleep(0.35)

    async def screenshot(self, scale=0.5) -> bytes | None:
        try:
            vp = await self.eval("({x: scrollX, y: scrollY, w: innerWidth, h: innerHeight})")
            r = await asyncio.wait_for(self.send("Page.captureScreenshot", {
                "format": "jpeg", "quality": 55, "clip": {"x": vp["x"], "y": vp["y"], "width": vp["w"], "height": vp["h"],
                                                          "scale": scale}}), 15)
            return base64.b64decode(r["data"])
        except Exception as e:
            log(f"capture impossible : {e.__class__.__name__}")
            return None

    async def refuse_cookies(self):
        js = r"""() => { const W = ['tout refuser','refuser tout','refuser','continuer sans accepter','reject all','reject',
          'only necessary','uniquement les cookies nécessaires','nécessaires uniquement','decline','je refuse','refuser les cookies non essentiels',
          'continue without accepting','use necessary cookies only'];
          const n = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
          const bs = [...document.querySelectorAll('button, a[role=button], [role=button]')];
          for (const w of W) { const b = bs.find(b => n(b.innerText) === w && b.offsetParent !== null); if (b) { b.click(); return w; } }
          return null; }"""
        try:
            return await self.eval(js, timeout=6)
        except Exception:
            return None

    async def close(self):
        await self.browser.close_tab(self)


# --------------------------------------------------------------------------------------- Browsers
class Browser:
    kind = "base"

    async def cdp(self, tid, method, params):
        raise NotImplementedError

    async def new_tab(self, url=None, lane=0) -> Tab:
        raise NotImplementedError

    async def tab_ids(self) -> list:
        raise NotImplementedError

    async def close_tab(self, tab):
        raise NotImplementedError

    async def openers(self) -> dict:
        return {}

    async def wait_new_tab(self, before: set, timeout=6.0, opener=None):
        t0 = time.time()
        while time.time() - t0 < timeout:
            ids = await self.tab_ids()
            new = [i for i in ids if i not in before]
            if new and opener is not None:
                op = await self.openers()
                if op:
                    new = [i for i in new if op.get(i) == opener]
            if new:
                t = Tab(self, new[-1])
                await t.wait_ready()
                return t
            await asyncio.sleep(0.15)
        return None

    async def open(self, url) -> Tab:
        t = await self.new_tab()
        await t.goto(url)
        return t


class ExtensionBridge:
    """Connexion WebSocket avec l'extension (côté serveur FastAPI)."""

    def __init__(self):
        self.ws = None
        self.pending: dict = {}
        self.ids = itertools.count(1)
        self.connected = asyncio.Event()
        self.version = None

    async def attach(self, ws):
        self.ws = ws
        self.connected.set()
        log("Extension Chrome connectée")
        st = load_settings()
        if not st.get("extension_seen"):
            from .config import save_settings
            st["extension_seen"] = True
            save_settings(st)

    def detach(self):
        self.ws = None
        self.connected.clear()
        for f in self.pending.values():
            if not f.done():
                f.set_exception(BrowserError("extension déconnectée"))
        self.pending.clear()
        log("Extension Chrome déconnectée")

    def on_message(self, msg: dict):
        if "id" in msg and msg["id"] in self.pending:
            f = self.pending.pop(msg["id"])
            if not f.done():
                if msg.get("error"):
                    f.set_exception(BrowserError(msg["error"]))
                else:
                    f.set_result(msg.get("result"))
        elif msg.get("event") == "hello":
            self.version = msg.get("version")

    async def call(self, cmd, timeout=60, **kw):
        if not self.ws:
            raise BrowserError("extension Chrome non connectée")
        i = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[i] = fut
        await self.ws.send_text(json.dumps({"id": i, "cmd": cmd, **kw}))
        return await asyncio.wait_for(fut, timeout)


BRIDGE = ExtensionBridge()


class ExtensionBrowser(Browser):
    kind = "extension"

    async def cdp(self, tid, method, params):
        return await BRIDGE.call("cdp", tabId=tid, method=method, params=params) or {}

    async def new_tab(self, url=None, lane=0):
        if load_settings().get("single_window", True):
            lane = 0  # une seule fenêtre StageAuto : LinkedIn et les autres sites y ouvrent leurs onglets côte à côte
        r = await BRIDGE.call("tabs.create", url="about:blank", lane=lane)
        t = Tab(self, r["tabId"])
        if url:  # navigation après coup : le curseur persistant est installé avant le chargement de la page
            await t.goto(url)
        return t

    async def tab_ids(self):
        return [t["tabId"] for t in await BRIDGE.call("tabs.list")]

    async def openers(self):
        return {t["tabId"]: t.get("opener") for t in await BRIDGE.call("tabs.list")}

    async def close_tab(self, tab):
        try:
            await BRIDGE.call("tabs.close", tabId=tab.id)
        except Exception:
            pass


class PlaywrightBrowser(Browser):
    kind = "playwright"

    def __init__(self):
        self.pw = None
        self.ctx = None
        self.pages = {}
        self.sessions = {}
        self.n = itertools.count(1)

    async def start(self):
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
        kw = dict(user_data_dir=str(CHROME_PROFILE), headless=bool(load_settings().get("headless")), viewport=None,
                  locale="fr-FR", args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
                  ignore_default_args=["--enable-automation"])
        exe = load_settings().get("chrome_path")
        if exe:
            self.ctx = await self.pw.chromium.launch_persistent_context(executable_path=exe, **kw)
        else:
            try:
                self.ctx = await self.pw.chromium.launch_persistent_context(channel="chrome", **kw)
            except Exception:
                self.ctx = await self.pw.chromium.launch_persistent_context(**kw)
        self.ctx.on("page", lambda p: self._register(p))
        for p in self.ctx.pages:
            self._register(p)

    def _register(self, page):
        for k, v in self.pages.items():
            if v is page:
                return k
        k = next(self.n)
        self.pages[k] = page
        page.on("close", lambda *_: self.pages.pop(k, None))
        return k

    async def cdp(self, tid, method, params):
        page = self.pages.get(tid)
        if page is None:
            raise BrowserError("onglet fermé")
        s = self.sessions.get(tid)
        if s is None:
            s = self.sessions[tid] = await self.ctx.new_cdp_session(page)
            try:
                await s.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            except Exception:
                pass
        return await s.send(method, params)

    async def new_tab(self, url=None, lane=0):
        p = await self.ctx.new_page()
        t = Tab(self, self._register(p))
        if url:
            await t.goto(url)
        return t

    async def tab_ids(self):
        return list(self.pages.keys())

    async def close_tab(self, tab):
        p = self.pages.pop(tab.id, None)
        self.sessions.pop(tab.id, None)
        if p:
            try:
                await p.close()
            except Exception:
                pass

    async def stop(self):
        try:
            if self.ctx:
                await self.ctx.close()
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass


_browser = None


class NoBrowser(Exception):
    """Extension non connectée et pas de Chrome dédié de secours : rien n'est fait, le passage suivant réessaie."""


def _chrome_running() -> bool:
    if not IS_WIN:
        return True
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"], capture_output=True, text=True,
                             timeout=15, creationflags=0x08000000).stdout.lower()
        return "chrome.exe" in out
    except Exception:
        return True


def _start_user_chrome() -> bool:
    """Chrome fermé : on lance TON Chrome (ton profil, ton extension), une seule fenêtre, jamais un second navigateur."""
    if not IS_WIN or _chrome_running():
        return False
    for exe in (os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
        if os.path.exists(exe):
            subprocess.Popen([exe])
            log("Chrome était fermé : ouverture de ton Chrome pour que l'extension se connecte")
            return True
    return False


async def get_browser(wait_ext=8.0, patient=False) -> Browser:
    """Extension si elle est connectée (ou le devient), sinon Chrome dédié via Playwright.
    patient : pour un passage, attend jusqu'à ~2 min une extension déjà utilisée auparavant (elle se reconnecte
    toute seule après le démarrage de l'application)."""
    global _browser
    mode = load_settings().get("browser_backend", "extension")
    if _browser is not None:
        if _browser.kind == "extension" and BRIDGE.ws:
            return _browser
        if _browser.kind == "playwright" and not (mode == "extension" and BRIDGE.ws):
            return _browser
        if _browser.kind == "playwright":  # l'extension est revenue : on repasse sur le vrai Chrome
            await _browser.stop()
        _browser = None
    if mode == "extension":
        wait = max(wait_ext, 130.0) if patient and load_settings().get("extension_seen") else wait_ext
        if not BRIDGE.ws and wait > 10:
            log(f"en attente de l'extension Chrome (jusqu'à {int(wait)} s)…")
        try:
            await asyncio.wait_for(BRIDGE.connected.wait(), wait)
            _browser = ExtensionBrowser()
            return _browser
        except asyncio.TimeoutError:
            pass
        if not load_settings().get("dedicated_chrome_fallback", False):
            # jamais de second Chrome : ton Chrome est ouvert s'il était fermé, sinon on attend l'extension
            if patient and _start_user_chrome():
                try:
                    await asyncio.wait_for(BRIDGE.connected.wait(), 90)
                    _browser = ExtensionBrowser()
                    return _browser
                except asyncio.TimeoutError:
                    pass
            raise NoBrowser("l'extension StageAuto Bridge n'est pas connectée : ouvre Chrome (ton profil) ; si elle ne se "
                            "reconnecte pas, clique son icône puis « Reconnecter »")
        log("Extension Chrome absente : repli sur le Chrome dédié (Playwright)")
    pb = PlaywrightBrowser()
    await pb.start()
    _browser = pb
    return _browser


async def reset_browser():
    global _browser
    if isinstance(_browser, PlaywrightBrowser):
        await _browser.stop()
    _browser = None
