#!/usr/bin/env python3
"""Export du plan de distribution électrique vers une carte Leaflet autonome.

Le script lit le schéma PostgreSQL `plan` (produit par `plan_electricite.py`)
et génère un fichier HTML unique contenant la carte Leaflet, les données
GeoJSON et la bibliothèque elle-même : le fichier s'ouvre par un simple
double-clic, sans serveur web ni accès disque supplémentaire.

Sous-commandes :
  export  : generate la carte autonome (visu/plan_kwilu.html par défaut)
  serve   : export puis sert le dossier sur http://localhost:8000
  check   : controle le fichier généré (JSON inline valide, couches non vides)

La configuration de la base provient du fichier .env (voir .env.example).
"""

import argparse
import base64
import json
import logging
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import text  # noqa: E402

from plan_electricite import SCHEMA, get_engine, setup_logging  # noqa: E402

VISU_DIR = BASE_DIR / "visu"
VENDOR_DIR = VISU_DIR / "vendor"
TEMPLATE = VISU_DIR / "template.html"
DEFAULT_OUT = VISU_DIR / "plan_kwilu.html"

LEAFLET_VERSION = "1.9.4"
LEAFLET_FILES = {
    "leaflet.js": f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.js",
    "leaflet.css": f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.css",
    "images/layers.png": f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/images/layers.png",
    "images/layers-2x.png": f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/images/layers-2x.png",
}

log = logging.getLogger("visu")


# --------------------------------------------------------------------------
# Extraction des couches
# --------------------------------------------------------------------------

def _collection(conn, sql: str) -> dict:
    """Exécute une requête renvoyant une FeatureCollection JSON."""
    row = conn.execute(text(sql)).scalar()
    if not row:
        return {"type": "FeatureCollection", "features": []}
    return json.loads(row)


SQL_PERIMETRE = f"""
SELECT json_build_object(
         'type', 'FeatureCollection',
         'features', COALESCE(json_agg(json_build_object(
             'type', 'Feature',
             'geometry', ST_AsGeoJSON(geom, 6)::json,
             'properties', json_build_object(
                 'agglomeration_id', agglomeration_id,
                 'rang', rang,
                 'nom', nom,
                 'population', population,
                 'superficie', superficie,
                 'centroid', ST_AsGeoJSON(centroid, 6)::json
             ))), '[]'::json)
       )::text
FROM {SCHEMA}.perimetre
"""

SQL_ZONE = f"""
SELECT json_build_object(
         'type', 'FeatureCollection',
         'features', COALESCE(json_agg(json_build_object(
             'type', 'Feature',
             'geometry', ST_AsGeoJSON(geom, 6)::json,
             'properties', json_build_object(
                 'agglomeration_id', agglomeration_id,
                 'zone_id', zone_id,
                 'methode', methode,
                 'nb_batiments', nb_batiments,
                 'surface_batie_m2', surface_batie_m2
             ))), '[]'::json)
       )::text
FROM {SCHEMA}.zone
"""

SQL_NOEUD = f"""
SELECT json_build_object(
         'type', 'FeatureCollection',
         'features', COALESCE(json_agg(json_build_object(
             'type', 'Feature',
             'geometry', ST_AsGeoJSON(geom, 6)::json,
             'properties', json_build_object(
                 'id', id,
                 'agglomeration_id', agglomeration_id,
                 'role', role,
                 'type_poste', type_poste,
                 'parent_noeud_id', parent_noeud_id,
                 'tension_kv', tension_kv,
                 'zone_id', zone_id,
                 'nb_batiments', nb_batiments,
                 'n_menages', n_menages,
                 'surface_batie_m2', surface_batie_m2,
                 'snap_distance_m', snap_distance_m
             ))), '[]'::json)
       )::text
FROM {SCHEMA}.noeud
"""

SQL_LIGNE = f"""
SELECT json_build_object(
         'type', 'FeatureCollection',
         'features', COALESCE(json_agg(json_build_object(
             'type', 'Feature',
             'geometry',
                 ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.00002), 5)::json,
             'properties', json_build_object(
                 'id', id,
                 'agglomeration_id', agglomeration_id,
                 'noeud_depart', noeud_depart,
                 'noeud_arrivee', noeud_arrivee,
                 'zone_id', zone_id,
                 'type_ligne', type_ligne,
                 'troncon_id', troncon_id,
                 'feeder_id', feeder_id,
                 'tension_kv', tension_kv,
                 'longueur_m', longueur_m,
                 'nb_aretes', nb_aretes,
                 'n_menages', n_menages,
                 'statut', statut,
                 'longueur_refusee_m', longueur_refusee_m,
                 'detour_ratio', detour_ratio
             ))), '[]'::json)
       )::text
FROM {SCHEMA}.ligne
"""


SQL_DERIVE = f"""
SELECT json_build_object(
         'type', 'FeatureCollection',
         'features', COALESCE(json_agg(json_build_object(
             'type', 'Feature',
             'geometry', ST_AsGeoJSON(geom, 6)::json,
             'properties', json_build_object(
                 'id', id,
                 'agglomeration_id', agglomeration_id,
                 'noeud_id', noeud_id,
                 'zone_id', zone_id,
                 'longueur_m', longueur_m,
                 'd_tronc_m', d_tronc_m
             ))), '[]'::json)
       )::text
FROM {SCHEMA}.derive
"""


def load_data() -> dict:
    """Charge les couches du plan depuis la base."""
    engine = get_engine()
    with engine.connect() as conn:
        a_derive = conn.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"{SCHEMA}.derive"}
        ).scalar()
        data = {
            "perimetre": _collection(conn, SQL_PERIMETRE),
            "zone": _collection(conn, SQL_ZONE),
            "noeud": _collection(conn, SQL_NOEUD),
            "ligne": _collection(conn, SQL_LIGNE),
            "derive": _collection(conn, SQL_DERIVE) if a_derive
            else {"type": "FeatureCollection", "features": []},
        }
    for name, coll in data.items():
        log.info("Couche %-10s : %d entités", name, len(coll["features"]))
    return data


# --------------------------------------------------------------------------
# Ressources HTML
# --------------------------------------------------------------------------

def ensure_vendor() -> None:
    """Télécharge Leaflet dans visu/vendor s'il n'est pas déjà présent."""
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    for rel, url in LEAFLET_FILES.items():
        dest = VENDOR_DIR / rel
        if dest.exists() and dest.stat().st_size > 0:
            continue
        log.info("Téléchargement %s …", rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as resp:
            dest.write_bytes(resp.read())


def _data_uri(path: Path) -> str:
    mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def read_leaflet() -> tuple[str, str]:
    """Retourne (css inliné, js inliné) avec les images du contrôle de calques."""
    css = (VENDOR_DIR / "leaflet.css").read_text(encoding="utf-8")
    for name in ("layers.png", "layers-2x.png"):
        img = VENDOR_DIR / "images" / name
        if img.exists():
            css = css.replace(f"url(images/{name})", f'url("{_data_uri(img)}")')
    js = (VENDOR_DIR / "leaflet.js").read_text(encoding="utf-8")
    return css, js


def build_html(data: dict) -> str:
    """Assemble le HTML autonome."""
    template = TEMPLATE.read_text(encoding="utf-8")
    css, js = read_leaflet()
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    html = template.replace("__LEAFLET_CSS__", css)
    html = html.replace("__LEAFLET_JS__", js)
    html = html.replace("__APP_JS__", (VISU_DIR / "app.js").read_text(encoding="utf-8"))
    html = html.replace("__APP_CSS__", (VISU_DIR / "style.css").read_text(encoding="utf-8"))
    html = html.replace("__DATA__", payload)
    return html


# --------------------------------------------------------------------------
# Sous-commandes
# --------------------------------------------------------------------------

def cmd_export(args) -> int:
    if not TEMPLATE.exists():
        log.error("Template introuvable : %s", TEMPLATE)
        return 1
    ensure_vendor()
    data = load_data()
    html = build_html(data)
    out = Path(args.out) if args.out else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log.info("Carte autonome écrite : %s (%.1f Mo)", out, out.stat().st_size / 1e6)
    log.info("À ouvrir par double-clic, ou via « %s serve ».", Path(__file__).name)
    return 0


def cmd_serve(args) -> int:
    import http.server
    import socketserver

    rc = cmd_export(args)
    if rc != 0:
        return rc

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(VISU_DIR), **kw)

        def log_message(self, fmt, *a):  # silence
            pass

    port = args.port
    with socketserver.TCPServer(("", port), Handler) as httpd:
        url = f"http://localhost:{port}/{DEFAULT_OUT.name}"
        log.info("Serveur démarré : %s (Ctrl+C pour arrêter)", url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("Serveur arrêté.")
    return 0


def cmd_check(args) -> int:
    out = Path(args.out) if args.out else DEFAULT_OUT
    if not out.exists():
        log.error("Fichier absent : %s", out)
        return 1
    html = out.read_text(encoding="utf-8")
    log.info("Fichier : %s (%.2f Mo)", out.name, out.stat().st_size / 1e6)

    marker = "window.DATA = "
    start = html.find(marker)
    if start < 0:
        log.error("Données introuvables dans le HTML.")
        return 1
    start += len(marker)
    end = html.find(";\n", start)
    if end < 0:
        log.error("Fin des données introuvable.")
        return 1
    try:
        data = json.loads(html[start:end])
    except json.JSONDecodeError as exc:
        log.error("JSON inline invalide : %s", exc)
        return 1
    log.info("JSON inline valide.")

    ok = True
    for name in ("perimetre", "zone", "noeud", "ligne"):
        coll = data.get(name, {})
        feats = coll.get("features", [])
        status = "OK" if feats else "VIDE"
        if not feats:
            ok = False
        log.info("  %-10s %4d entités  %s", name, len(feats), status)

    for marker_txt in ("__LEAFLET_CSS__", "__LEAFLET_JS__", "__APP_JS__", "__APP_CSS__", "__DATA__"):
        if marker_txt in html:
            log.error("Placeholder non remplacé : %s", marker_txt)
            ok = False

    externes = [u for u in ("unpkg.com", "cdn.jsdelivr.net", "cdnjs.") if u in html]
    if externes:
        log.warning("Référence CDN encore présente : %s", ", ".join(externes))
    else:
        log.info("Aucune dépendance CDN : fichier autonome.")

    if "tile.openstreetmap.org" in html:
        log.info("Fond de carte : tuiles OpenStreetMap (bouton « Sans fond » disponible).")

    log.info("Contrôle : %s", "OK" if ok else "ÉCHEC")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export du plan de distribution vers une carte Leaflet autonome."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="génère le fichier HTML autonome")
    p_exp.add_argument("--out", default=None, metavar="FICHIER",
                        help=f"fichier de sortie (défaut : {DEFAULT_OUT.name})")
    p_exp.set_defaults(func=cmd_export)

    p_srv = sub.add_parser("serve", help="export puis serveur local")
    p_srv.add_argument("--out", default=None, metavar="FICHIER")
    p_srv.add_argument("--port", type=int, default=8000)
    p_srv.set_defaults(func=cmd_serve)

    p_chk = sub.add_parser("check", help="contrôle le fichier généré")
    p_chk.add_argument("--out", default=None, metavar="FICHIER")
    p_chk.set_defaults(func=cmd_check)

    args = parser.parse_args()
    setup_logging()
    try:
        return args.func(args) or 0
    except Exception:
        log.exception("Échec de l'export de la carte")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
