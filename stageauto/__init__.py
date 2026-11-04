"""StageAuto : candidatures de stage automatisées."""
import hashlib
from pathlib import Path


def code_fingerprint() -> str:
    """Empreinte du contenu du code. Si une instance déjà lancée a une empreinte différente (nouvelle version
    décompressée par-dessus), StageAuto.bat l'arrête et démarre la nouvelle."""
    root = Path(__file__).resolve().parent
    h = hashlib.sha1()
    for p in sorted(list(root.rglob("*.py")) + list(root.rglob("*.html"))):
        if "__pycache__" in p.parts:
            continue
        h.update(p.relative_to(root).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


CODE = code_fingerprint()
