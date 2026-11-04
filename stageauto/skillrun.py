"""Lance build.py du skill comme sous Linux, quelle que soit la langue de Windows.

python -X utf8 skillrun.py <build.py> <arguments de build.py>

Sous Windows, la sortie d'un programme lancé par un autre passe par l'encodage cp1252 : le premier print("✓ …") de
build.py plantait (UnicodeEncodeError), avant même d'afficher les vraies erreurs. Ici les sorties sont en UTF-8, et
ce que renvoient pdflatex, pdftotext… est lu en UTF-8 tolérant (un octet inattendu ne fait plus planter la lecture).
"""
import os
import runpy
import subprocess
import sys

_run = subprocess.run


def _tolerant(*a, **kw):
    if kw.get("text") or kw.get("universal_newlines"):
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("errors", "replace")
    if sys.platform == "win32":
        kw.setdefault("creationflags", 0x08000000)  # pas de fenêtre noire pour pdflatex et les outils PDF
    return _run(*a, **kw)


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    subprocess.run = _tolerant
    script = os.path.abspath(sys.argv[1])
    sys.argv = sys.argv[1:]
    sys.path[0] = os.path.dirname(script)  # comme « python build.py » : le dossier du script, pas celui de StageAuto
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
