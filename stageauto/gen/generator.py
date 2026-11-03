"""CV + lettre ciblés depuis les modèles par domaine (logique de gen.py, compatible Windows/MiKTeX).

generate(o, out_dir) -> dict(cv=..., lm=..., lettre_txt=..., warnings=[...])
o = objet produit par llm.tailor (e, s, dom, poste, domaine, equipe, ville, accroche, pourquoi, kw, puces, skills,
savoir, preuve, lang).
"""
import datetime
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..config import BANNED_TOOLS, IS_WIN, SKILL_DIR, TPL_DIR, WORK

HERE = Path(__file__).resolve().parent
TPL = TPL_DIR
SKILL = SKILL_DIR
ASSETS = SKILL / "assets"
TEXMF = SKILL / "texmf"
MOIS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre',
        'novembre', 'décembre']
MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
          'November', 'December']


def esc(t: str) -> str:
    t = re.sub(r'\s*[\(\[]?\b[FH]\s*/\s*[HF]\b[\)\]]?', '', t or '')
    t = t.replace('–', '-').replace('—', '-').replace('--', '-')
    for a, b in [('\\', ''), ('&', '\\&'), ('%', '\\%'), ('#', '\\#'), ('_', ' '), ('$', '')]:
        t = t.replace(a, b)
    return t.strip()


def esc_item(t: str) -> str:
    """Puce déjà au format LaTeX (\\kw{..}, \\&) : on garde les macros, on neutralise le reste."""
    t = (t or '').replace('–', '-').replace('—', '-').replace('--', '-')
    t = re.sub(r'(?<!\\)&', r'\\&', t)
    t = re.sub(r'(?<!\\)%', r'\\%', t)
    t = re.sub(r'(?<!\\)#', r'\\#', t)
    t = re.sub(r'\\(?!kw\{|&|%|#)', '', t)
    return t.strip()


def plain(tex: str) -> str:
    ps = re.findall(r'\\lmpar\{(.*?)\}\n', tex)
    s = '\n\n'.join(ps)
    for a, b in [('\\&', '&'), ('\\%', '%'), ('\\#', '#'), ('~', ' '), ('\\textendash', '-')]:
        s = s.replace(a, b)
    return re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', s)


def slug(t: str) -> str:
    return re.sub(r"[^\w]+", '_', t or '', flags=re.U).strip('_')[:40]


def latex_bin():
    for b in ("pdflatex",):
        p = shutil.which(b)
        if p:
            return p
    if IS_WIN:
        for c in [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/MiKTeX/miktex/bin/x64/pdflatex.exe",
                  Path("C:/Program Files/MiKTeX/miktex/bin/x64/pdflatex.exe")]:
            if c.exists():
                return str(c)
    return None


def _env():
    env = dict(os.environ)
    sep = ';' if IS_WIN else ':'
    t = str(TEXMF)
    env["TEXINPUTS"] = sep.join(['.', str(ASSETS), f"{t}/tex//", ''])
    env["TFMFONTS"] = sep.join(['.', f"{t}/fonts/tfm//", ''])
    env["VFFONTS"] = sep.join(['.', f"{t}/fonts/vf//", ''])
    env["T1FONTS"] = sep.join(['.', f"{t}/fonts/type1//", ''])
    env["ENCFONTS"] = sep.join(['.', f"{t}/fonts/enc//", ''])
    env["TEXFONTMAPS"] = sep.join(['.', f"{t}/fonts/map//", ''])
    return env


def compile_tex(texfile: Path) -> tuple[bool, str]:
    exe = latex_bin()
    if not exe:
        return False, "pdflatex introuvable : lance install.bat (MiKTeX)"
    job = texfile.stem
    cmd = [exe, "-interaction=nonstopmode", "-halt-on-error", f"-jobname={job}",
           "\\pdfmapfile{+SourceSansPro.map}\\input{" + texfile.name + "}"]
    try:
        r = subprocess.run(cmd, cwd=texfile.parent, env=_env(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180,
                           creationflags=0x08000000 if IS_WIN else 0)
    except subprocess.TimeoutExpired:
        return False, "pdflatex : délai dépassé"
    pdf = texfile.with_suffix(".pdf")
    if r.returncode != 0 or not pdf.exists():
        err = [l for l in r.stdout.splitlines() if l.startswith("!")][:3]
        return False, "pdflatex : " + " | ".join(err or [r.stdout[-300:]])
    return True, ""


def pages(pdf: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf)).pages)


ACTIF = ("/OpenAction", "/AA", "/Names", "/AcroForm", "/Outlines", "/PageMode", "/JavaScript")


def neutralise(pdf: Path) -> None:
    """Retire le contenu actif refusé par certains ATS ; garde seulement les liens URI ; PDF 1.4 sans flux d'objets."""
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, NameObject
    w = PdfWriter(clone_from=str(pdf))
    root = w._root_object
    for k in ACTIF:
        if k in root:
            del root[k]
    for page in w.pages:
        if "/AA" in page:
            del page["/AA"]
        annots = page.get("/Annots")
        if annots is None:
            continue
        keep = ArrayObject()
        for a in annots:
            o = a.get_object()
            act = o.get("/A")
            act = act.get_object() if act is not None else None
            if o.get("/Subtype") == "/Link" and act is not None and act.get("/S") == "/URI":
                keep.append(a)
        if keep:
            page[NameObject("/Annots")] = keep
        else:
            del page["/Annots"]
    with open(pdf, "wb") as f:
        w.write(f)
    try:
        import pikepdf
        tmp = pdf.with_suffix(".q.pdf")
        with pikepdf.open(str(pdf)) as p:
            p.remove_unreferenced_resources()
            p.save(str(tmp), object_stream_mode=pikepdf.ObjectStreamMode.disable, force_version="1.4",
                   fix_metadata_version=False)
        os.replace(tmp, pdf)
    except Exception:
        pass


def ats_missing(pdf: Path, kws: list[str]) -> list[str]:
    try:
        from pypdf import PdfReader
        t = " ".join(p.extract_text() or "" for p in PdfReader(str(pdf)).pages).lower()
        t = re.sub(r"\s+", " ", t.replace("\u2019", "'").replace("\u00a0", " "))
        return [k for k in kws if k.lower() not in t]
    except Exception:
        return []


def _shrink(cv: str) -> str | None:
    """Débordement : retire le dernier projet, puis la dernière puce de l'entrée la plus longue."""
    proj = cv.split("\\section{Projets}") if "\\section{Projets}" in cv else cv.split("\\section{Projects}")
    if len(proj) == 2:
        items = re.findall(r"\\item \\textbf[^\n]*\n", proj[1])
        if len(items) > 3:
            return proj[0] + ("\\section{Projets}" if "\\section{Projets}" in cv else "\\section{Projects}") + \
                proj[1].replace(items[-1], "", 1)
    blocks = re.findall(r"\\begin\{bullets\}\n((?:\\item (?!\\textbf)[^\n]*\n)+)\\end\{bullets\}", cv)
    blocks = [b for b in blocks if b.count("\\item") > 2]
    if blocks:
        b = max(blocks, key=lambda b: b.count("\\item"))
        last = b.strip().split("\n")[-1] + "\n"
        return cv.replace(b, b.replace(last, "", 1), 1)
    return None


def build_tex(o: dict) -> tuple[str, str, list[str]]:
    warns = []
    dom = o["dom"] if (TPL / f"cv_{o.get('dom')}.tex").exists() else "soc"
    cv = (TPL / f"cv_{dom}.tex").read_text(encoding="utf-8")
    cv = cv.replace('@@POSTE@@', esc(o['poste'])).replace('@@DOMAINE@@', esc(o['domaine']))
    for p in o.get('puces', []):
        old, new = (p['old'], p['new']) if isinstance(p, dict) else p
        new = esc_item(new)
        if re.search(r"LLM", new) and "en cours" not in new:
            new = new.replace("LLM", "IA")
        m = re.search(r'\\item (?!\\textbf)' + re.escape(old.strip()[:60]) + r'[^\n]*', cv)
        if m:
            cv = cv.replace(m.group(0), '\\item ' + new)
        else:
            warns.append('puce introuvable: ' + old[:40])

    def cap2(m):
        n = [0]

        def u(k):
            n[0] += 1
            return k.group(0) if n[0] <= 2 else k.group(1)
        return re.sub(r'\\kw\{([^}]*)\}', u, m.group(0))
    cv = re.sub(r'\\item [^\n]*', cap2, cv)
    skills = o.get('skills') or []
    skills = {s['label']: s['value'] for s in skills} if isinstance(skills, list) else dict(skills)
    for lab, val in skills.items():
        val = re.sub(r'^[^}]*\}\{', '', val)[:88]
        cv = re.sub(r'(\\skillline\{' + re.escape(lab) + r'\})\{[^\n]*\}', lambda m: m.group(1) + '{' + esc(val) + '}', cv)
    if o.get('savoir'):
        cv = re.sub(r'(\\skillline\{Savoir-être\})\{[^\n]*\}', lambda m: m.group(1) + '{' + esc(o['savoir'])[:80] + '}', cv)
    kws = [k for k in o.get('kw', []) if not any(b.lower() == k.lower() for b in BANNED_TOOLS)]
    low = cv.lower()
    miss = [esc(k) for k in kws if esc(k).lower() not in low and k.lower() not in low]
    if miss and '\\section{Compétences}' in cv:
        sec = cv.split('\\section{Compétences}')[1].split('\\section')[0]
        lines = [l for l in re.findall(r'\\skillline\{[^}]*\}\{[^\n]*\}', sec)
                 if 'Savoir' not in l and not any('{%s}' % lab in l for lab in skills)]
        kl = [k.lower() for k in kws]
        lines.sort(key=lambda l: sum(k in l.lower() for k in kl))
        for tgt in lines:
            if not miss:
                break
            lab, val = re.match(r'\\skillline\{([^}]*)\}\{(.*)\}$', tgt).groups()
            old = [v.strip() for v in val.split(',')]
            keep = [v for v in old if any(k in v.lower() for k in kl)]
            if not keep:
                lab = 'Technologies'
            out, placed = '', []
            for it in keep + miss + [v for v in old if v not in keep]:
                cand = (out + ', ' + it) if out else it
                if len(lab) + len(cand) <= 88:
                    out = cand
                    if it in miss:
                        placed.append(it)
            miss = [m for m in miss if m not in placed]
            cv = cv.replace(tgt, '\\skillline{%s}{%s}' % (lab, out))
        if miss:
            warns.append('kw non placés: ' + ', '.join(miss))
    lm = (TPL / f"lm_{dom}.tex").read_text(encoding="utf-8")
    if o.get('preuve'):
        ps = re.findall(r'\\lmpar\{[^\n]*\}', lm)
        if len(ps) > 2:
            lm = lm.replace(ps[2], '\\lmpar{' + esc(o['preuve']) + '}')
    d = datetime.date.today()
    rep = {'ENTREPRISE': o['e'], 'EQUIPE': o.get('equipe') or 'Service recrutement', 'VILLE': o.get('ville') or 'Paris',
           'OBJET': o['poste'], 'ACCROCHE': o['accroche'], 'POURQUOI': o['pourquoi']}
    for k, v in rep.items():
        lm = lm.replace('@@%s@@' % k, esc(v))
    lm = lm.replace('@@DATE@@', f"{d.day} {MOIS[d.month - 1]} {d.year}")
    return cv, lm, warns


def generate(o: dict, out_dir: Path, english_rules: str | None = None, translate=None) -> dict:
    E = slug(o['e']) + '_' + slug(o['s'])
    w = WORK / E
    if w.exists():
        shutil.rmtree(w, ignore_errors=True)
    w.mkdir(parents=True, exist_ok=True)
    for f in ASSETS.iterdir():
        if f.is_file():
            shutil.copy(f, w)
        else:
            shutil.copytree(f, w / f.name, dirs_exist_ok=True)
    cv, lm, warns = build_tex(o)
    en = o.get('lang') == 'en' and translate is not None
    if en:
        cv_en, lm_en = translate(cv, english_rules or ""), translate(lm, english_rules or "")
        if cv_en and lm_en:
            cv, lm = cv_en, lm_en
        else:
            en = False
            warns.append("traduction anglaise échouée : version française")
    ent = slug(o['e'])
    cvf = w / f"CV_Abderraouf_Derardja_{ent}.tex"
    lmf = w / (f"Cover_Letter_Abderraouf_Derardja_{ent}.tex" if en else f"Lettre_Abderraouf_Derardja_{ent}.tex")
    lmf.write_text(lm, encoding="utf-8")
    ok, err = compile_tex(lmf)
    if not ok:
        return {"ok": False, "error": "lettre " + err, "warnings": warns}
    for attempt in range(4):
        cvf.write_text(cv, encoding="utf-8")
        ok, err = compile_tex(cvf)
        if not ok:
            return {"ok": False, "error": "CV " + err, "warnings": warns}
        if pages(cvf.with_suffix(".pdf")) == 1:
            break
        cv2 = _shrink(cv)
        if not cv2:
            return {"ok": False, "error": "CV sur 2 pages", "warnings": warns}
        cv = cv2
        warns.append("CV raccourci (débordement)")
    if pages(lmf.with_suffix(".pdf")) != 1:
        warns.append("lettre sur 2 pages")
    out_dir.mkdir(parents=True, exist_ok=True)
    res = {"ok": True, "warnings": warns, "folder": str(out_dir)}
    for key, f in (("cv", cvf), ("lm", lmf)):
        pdf = f.with_suffix(".pdf")
        neutralise(pdf)
        dst = out_dir / pdf.name
        shutil.copy(pdf, dst)
        res[key] = str(dst)
    miss = ats_missing(Path(res["cv"]), [k for k in o.get('kw', []) if k not in BANNED_TOOLS][:15])
    if miss:
        res["warnings"].append("ATS: mots-clés non lus " + ", ".join(miss[:6]))
    txt = plain(lm)
    (out_dir / "lettre.txt").write_text(txt, encoding="utf-8")
    res["lettre_txt"] = txt
    return res
