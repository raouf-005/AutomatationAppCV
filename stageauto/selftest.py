"""Auto-test d'installation : LaTeX, outils PDF, base, extension (python -m stageauto.selftest)."""
import shutil
import tempfile
from pathlib import Path

from . import db
from .gen import generator


def main():
    ok = True
    print("LaTeX (pdflatex) :", generator.latex_bin() or "ABSENT")
    for t in ("qpdf", "pdftotext", "pdfinfo", "pdffonts"):
        print(f"{t:10s}:", "OK" if shutil.which(t) else "absent (contrôle ATS allégé)")
    o = {"e": "Test", "s": "Installation", "dom": "soc", "poste": "Analyste SOC", "domaine": "détection et réponse à incident",
         "equipe": "SOC", "ville": "Paris", "accroche": "Test de compilation.", "pourquoi": "Test de compilation.",
         "kw": ["SIEM", "EDR"], "puces": [], "skills": [], "savoir": "rigueur", "preuve": "", "lang": "fr"}
    out = Path(tempfile.mkdtemp())
    r = generator.generate(o, out)
    print("CV d'essai :", "OK" if r.get("ok") else f"ÉCHEC {r.get('error')}")
    ok &= bool(r.get("ok"))
    db.import_seed()
    print("Historique importé :", db.q("SELECT COUNT(*) n FROM offers")[0]["n"], "offres")
    print("\nInstallation", "OK" if ok else "INCOMPLÈTE (voir ci-dessus ; relance install.bat après le redémarrage de MiKTeX)")


if __name__ == "__main__":
    main()
