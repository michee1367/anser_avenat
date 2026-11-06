"""Vérification d'une table PostGIS chargée par l'ETL."""
import sys

from sqlalchemy import text

from etl import get_engine, setup_logging

table = sys.argv[1] if len(sys.argv) > 1 else "batiments_rdc_test"
setup_logging()
engine = get_engine()
with engine.connect() as conn:
    n, srid = conn.execute(
        text(f'SELECT count(*), ST_SRID(geometry) FROM "{table}" GROUP BY 2')
    ).first()
    print(f"Table {table}: {n:,} lignes | SRID: {srid}")
    xmin, ymin, xmax, ymax = conn.execute(
        text(
            f'SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) '
            f'FROM (SELECT ST_Extent(geometry) AS e FROM "{table}") s'
        )
    ).first()
    print(f"Emprise: ({xmin:.1f}, {ymin:.1f}) → ({xmax:.1f}, {ymax:.1f})")
    rows = conn.execute(
        text(f'SELECT * FROM "{table}" LIMIT 2')
    ).fetchall()
    print("Colonnes:", list(rows[0]._mapping.keys()) if rows else "aucune")
