"""Test hors ligne : couche navigateur + remplissage automatique + boucle de l'agent (modèle simulé) sur tests/form.html.
Usage : python tests/test_local.py   (variable STAGEAUTO_CHROME = chemin d'un Chrome/Chromium, sinon Chrome installé)"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STAGEAUTO_DATA", tempfile.mkdtemp())
from stageauto import agent, cdp, config, forms, llm  # noqa: E402

s = config.load_settings()
s.update(browser_backend="playwright", headless=True, chrome_path=os.environ.get("STAGEAUTO_CHROME", ""))
config.save_settings(s)
TMP = Path(os.environ["STAGEAUTO_DATA"])
(TMP / "cv.pdf").write_bytes(b"%PDF-1.4\n%test\n")
(TMP / "lm.pdf").write_bytes(b"%PDF-1.4\n%test\n")
FILES = {"cv": str(TMP / "cv.pdf"), "lm": str(TMP / "lm.pdf"), "lettre_txt": "Madame, Monsieur, ..."}
URL = (ROOT / "tests" / "form.html").as_uri()


class FakeMsgs:
    """Modèle simulé : auto_fill -> clic sur « Envoyer ma candidature » -> finish."""
    def __init__(self):
        self.n = 0

    def create(self, **kw):
        self.n += 1
        if self.n == 1:
            step = [("auto_fill", {})]
        elif self.n == 2:
            res = kw["messages"][-1]["content"][0]["content"]
            ref = [l.split()[0] for l in res.splitlines() if "Envoyer ma candidature" in l][0]
            step = [("click", {"ref": ref})]
        else:
            step = [("finish", {"status": "applied", "reason": "ok", "method_note": "test", "contacts": []})]
        return NS(content=[NS(type="tool_use", id=f"t{self.n}{i}", name=n, input=a) for i, (n, a) in enumerate(step)],
                  stop_reason="tool_use")


async def main():
    b = await cdp.get_browser(wait_ext=0.1)
    t = await b.new_tab(URL)
    rep = await forms.auto_fill(t, {**FILES, "source": "Test"}, {"company": "T", "title": "Stage"}, use_llm=False)
    assert not rep["unanswered_required"], rep
    vals = await t.eval("() => Object.fromEntries([...new FormData(document.getElementById('f')).entries()]"
                        ".map(([k, v]) => [k, typeof v === 'string' ? v : v.name]))")
    assert vals["civ"] == "Monsieur" and vals["am"] == "n" and vals["cv"] == "cv.pdf" and "nl" not in vals, vals
    print("auto_fill OK :", json.dumps(vals, ensure_ascii=False))
    t2 = await b.new_tab(URL)
    llm._client = NS(beta=NS(messages=FakeMsgs()))
    r = await agent.Agent(b, t2, "test", FILES, {"company": "T", "title": "Stage", "source": "Test"}).run("Postule.")
    assert r["status"] == "applied", r
    assert forms.looks_success(await t2.url(), await t2.text())
    print("agent OK :", r["status"], r["steps"], "étapes")
    await cdp.reset_browser()


asyncio.run(main())
