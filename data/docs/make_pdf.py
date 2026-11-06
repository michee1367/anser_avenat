#!/usr/bin/env python3
"""Génère les PDF des deux documentations depuis les fichiers HTML.

Aucune dépendance Python : le rendu est confié à Chrome (ou Edge) en mode
headless, qui applique la feuille de style d'impression (docs/style.css).

    python docs/make_pdf.py            # les deux documents
    python docs/make_pdf.py logiciel   # un seul document
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ICI = Path(__file__).resolve().parent
DOCUMENTS = {
    "logiciel": ICI / "ingenieur_logiciel.html",
    "electricien": ICI / "ingenieur_electricien.html",
}

CHROME_LINUX = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
]
CHROME_WINDOWS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _chrome() -> str:
    for chemin in (CHROME_WINDOWS if os.name == "nt" else CHROME_LINUX):
        if Path(chemin).exists():
            return chemin
    raise SystemExit("Chrome ou Edge est introuvable.")


def _win(path: Path) -> str:
    """Chemin Windows (slashes) pour un exécutable Windows."""
    p = str(path.resolve())
    if os.name == "nt":
        return p.replace("\\", "/")
    if p.startswith("/mnt/") and len(p) > 6:
        lecteur = p[5].upper()
        return f"{lecteur}:/{p[7:]}"
    raise SystemExit(f"Chemin non convertible en Windows : {p}")


def _pages(pdf: Path) -> str:
    for outil, args in (
        ("qpdf", ["--show-npages"]),
        (None, None),
    ):
        if outil and shutil.which(outil):
            try:
                res = subprocess.run([outil, *args, str(pdf)],
                                     capture_output=True, text=True, timeout=30)
                if res.returncode == 0 and res.stdout.strip():
                    return f"{res.stdout.strip()} pages"
            except Exception:
                pass
    try:
        data = pdf.read_bytes()
        n = len(re.findall(rb"/Type\s*/Page[^s]", data))
        if n:
            return f"{n} pages"
    except Exception:
        pass
    return "pages ?"


def convertir(cle: str, chrome: str) -> int:
    html = DOCUMENTS[cle]
    if not html.exists():
        raise SystemExit(f"Fichier absent : {html}")
    pdf = html.with_suffix(".pdf")
    with tempfile.TemporaryDirectory(prefix="_chrome_", dir=ICI) as profil:
        cmd = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--virtual-time-budget=5000",
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
            f"--user-data-dir={_win(Path(profil))}",
            f"--print-to-pdf={_win(pdf)}",
            "file:///" + _win(html),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not pdf.exists() or pdf.stat().st_size == 0:
        sys.stderr.write(res.stdout + res.stderr)
        raise SystemExit(f"Échec de génération : {pdf}")
    taille = pdf.stat().st_size / 1024 / 1024
    print(f"  {pdf.name:34s} {taille:5.2f} Mo   {_pages(pdf)}")
    return 0


def main(argv: list[str]) -> int:
    cles = argv[1:] or list(DOCUMENTS)
    inconnues = [c for c in cles if c not in DOCUMENTS]
    if inconnues:
        raise SystemExit(f"Document inconnu : {', '.join(inconnues)}")
    chrome = _chrome()
    print(f"Chrome : {chrome}")
    for cle in cles:
        convertir(cle, chrome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
