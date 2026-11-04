"""Gmail (dans Chrome) : e-mails des dernières 24 h demandant une action, confirmations d'adresse, réponses de recruteurs."""
import asyncio
import re

from .config import load_settings
from .notify import log

ROWS_JS = r"""() => [...document.querySelectorAll('tr.zA')].slice(0, 60).map((r, i) => {
  const f = r.querySelector('.yX .yW [email], .yW span') || {};
  return {i, from: (f.getAttribute && f.getAttribute('email')) || '', name: (f.innerText || '').trim(),
          subject: ((r.querySelector('.bog') || {}).innerText || '').trim(), snippet: ((r.querySelector('.y2') || {}).innerText || '').trim()}; })"""

ACTION = re.compile(r"entretien|interview|test technique|assessment|disponibilit|rendez-vous|visio|échange t[ée]l[ée]phonique|"
                    r"next steps?|prochaine [ée]tape|convocation|case study|coding (test|challenge)", re.I)
CONFIRM = re.compile(r"confirm(ez|er)? (votre|ton|your) (adresse|e-?mail|candidature)|v[ée]rifi(ez|er) (votre|your) (adresse|e-?mail)|"
                     r"finalisez votre candidature|verify your email", re.I)
REFUS = re.compile(r"malheureusement|unfortunately|ne pas donner suite|pas retenu|not (been )?selected|regret", re.I)
ACK = re.compile(r"candidature (a bien été |est )?(reçue|envoyée|transmise)|bien reçu votre candidature|application (received|submitted)|"
                 r"thank you for applying|merci pour votre candidature", re.I)
ALERT = re.compile(r"alerte|alert|newsletter|offres? (du jour|similaires)|jobs? for you|recommand|nouvelles offres", re.I)


async def check(browser):
    idx = load_settings().get("gmail_index", 0)
    res = {"actions": [], "confirmed": [], "refus": [], "replies": [], "acks": [], "login": False}
    base = f"https://mail.google.com/mail/u/{idx}/#search/newer_than%3A1d"
    tab = await browser.new_tab(base)
    try:
        await tab.wait_for("() => document.querySelectorAll('tr.zA').length || /accounts\\.google/.test(location.host)"
                           " || /Aucun message|No messages/.test(document.body.innerText)", timeout=12)
        await tab.tour("tr.zA", "E-mail", 4)
        if "accounts.google.com" in (await tab.url()):
            res["login"] = True
            return res
        for r in await tab.eval(ROWS_JS) or []:
            txt = f"{r['subject']} {r['snippet']}"
            who = r["name"] or r["from"]
            if CONFIRM.search(txt):
                ok = await _confirm(browser, tab, r["i"])
                if ok:
                    res["confirmed"].append(f"{who} : {r['subject'][:80]}")
                await tab.goto(base)
                await tab.wait_for("() => document.querySelectorAll('tr.zA').length", timeout=10)
            elif ACTION.search(txt) and not ACK.search(txt):
                res["actions"].append(f"{who} : {r['subject'][:90]}")
            elif REFUS.search(txt):
                res["refus"].append(f"{who} : {r['subject'][:80]}")
            elif ACK.search(txt):  # accusé de réception : sert à confirmer qu'une candidature est bien arrivée
                res["acks"].append(f"{who} {r['subject']} {r['snippet'][:120]}")
            elif not ALERT.search(txt) and re.search(r"stage|internship|candidature|application|recrut", txt, re.I):
                res["replies"].append(f"{who} : {r['subject'][:80]}")
    except Exception as e:
        log(f"Gmail : {e.__class__.__name__}")
    finally:
        await tab.close()
    return res


async def _confirm(browser, tab, i) -> bool:
    """Ouvre l'e-mail et suit le lien « Confirmer / Vérifier » (dans un onglet séparé)."""
    try:
        await tab.eval("(i) => { const r = document.querySelectorAll('tr.zA')[i]; if (r) r.click(); }", i)
        await asyncio.sleep(3)
        href = await tab.eval(r"""() => { const a = [...document.querySelectorAll('div.a3s a[href]')].find(a =>
            /confirm|v[ée]rifi|finalis|activer|verify/i.test(a.innerText + ' ' + a.href)); return a ? a.href : null; }""")
        if not href:
            return False
        t2 = await browser.new_tab(href)
        await asyncio.sleep(5)
        await t2.close()
        return True
    except Exception:
        return False
