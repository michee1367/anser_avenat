# -*- coding: utf-8 -*-
import io

p = 'plan_electricite.py'
s = io.open(p, encoding='utf-8').read()


def rep(a, b):
    global s
    assert s.count(a) == 1, (s.count(a), a[:90])
    s = s.replace(a, b)


# --- A. Placement : centroid pour l'affectation, sommet pour le réseau.
rep('''    # 7. Recalage des postes dans la composante de service, puis un seul
    #    poste par sommet routier (un sommet partagé n'aurait qu'un seul
    #    point de raccordement MT).
    conn.execute(text("DROP TABLE IF EXISTS _poste_vertex"))
    conn.execute(text("CREATE TEMP TABLE _poste_vertex (vid bigint PRIMARY KEY)"))
    conn.execute(text("INSERT INTO _poste_vertex (vid) VALUES (:v)"), {"v": vertex})
    final_snaps = []
    n_recalage = n_reloges = n_fusion = 0
    for p, v, d, xy in snaps:
        if comp is not None:
            v2, d2, xy2 = _nearest_vertex(conn, p["cx"], p["cy"], component=comp)
            if v2 is not None:
                if v2 != v:
                    n_recalage += 1
                v, d, xy = v2, d2, xy2
        libre = conn.execute(text(
            "INSERT INTO _poste_vertex (vid) VALUES (:v) "
            "ON CONFLICT (vid) DO NOTHING RETURNING vid"), {"v": v}).first()
        if libre is None:
            joint = (f"JOIN {SCHEMA}.road_component c ON c.node = v.id"
                     if comp is not None else "")
            filtre = "c.component = :comp AND " if comp is not None else ""
            alt = conn.execute(text(f"""
                SELECT v.id, ST_X(v.geom) AS x, ST_Y(v.geom) AS y,
                       ST_Distance(v.geom::geography,
                           ST_SetSRID(ST_MakePoint(:px,:py),4326)::geography) AS d
                FROM {SCHEMA}.road_vertex v
                {joint}
                WHERE {filtre}NOT EXISTS (
                    SELECT 1 FROM _poste_vertex u WHERE u.vid = v.id)
                  AND ST_DWithin(v.geom::geography,
                      ST_SetSRID(ST_MakePoint(:px,:py),4326)::geography, :rad)
                ORDER BY v.geom <-> ST_SetSRID(ST_MakePoint(:px,:py),4326)
                LIMIT 1
            """), {"px": p["cx"], "py": p["cy"], "comp": comp,
                   "rad": float(args.max_snap_m) * 2.0}).mappings().first()
            if alt is None:
                log.warning("  poste (zone %d) : aucun sommet libre à %.0f m, "
                            "poste fusionné avec le sommet voisin.",
                            p["zone_id"], 2.0 * args.max_snap_m)
                n_fusion += 1
                continue
            else:
                v, d, xy = alt["id"], float(alt["d"]), (alt["x"], alt["y"])
                conn.execute(text("INSERT INTO _poste_vertex (vid) VALUES (:v)"),
                             {"v": v})
                n_reloges += 1
        final_snaps.append((p, v, d, xy))''',
'''    # 7. Recalage des postes dans la composante de service. Plusieurs postes
    #    peuvent partager un même sommet routier : le réseau MT est alors
    #    mutualisé sur ce sommet, chaque poste gardant son propre tronc BT.
    final_snaps = []
    n_recalage = 0
    for p, v, d, xy in snaps:
        if comp is not None:
            v2, d2, xy2 = _nearest_vertex(conn, p["cx"], p["cy"], component=comp)
            if v2 is not None and d2 is not None and d2 <= args.max_snap_m:
                if v2 != v:
                    n_recalage += 1
                v, d, xy = v2, d2, xy2
        final_snaps.append((p, v, d, xy))''')

rep('''             "et %d bâtiments (%d recalées, %d relogées, %d fusionnées, "
             "plafond %d ménages/poste, MT %.1f kV)",
             aid, agg["nom"], args.tension_source, vertex, comp, dist,
             len(final_snaps), len(zones), n_bat, n_recalage, n_reloges,
             n_fusion, args.max_menages_poste, _tension_mt(args))''',
'''             "et %d bâtiments (%d recalées, plafond %d ménages/poste, "
             "MT %.1f kV)",
             aid, agg["nom"], args.tension_source, vertex, comp, dist,
             len(final_snaps), len(zones), n_bat, n_recalage,
             args.max_menages_poste, _tension_mt(args))''')

# --- B. Affectation des bâtiments au centroïde du groupe (équilibrée).
rep('''        SELECT n.id AS noeud_id, n.zone_id, b.g, b.g33
        FROM _bat b
        CROSS JOIN LATERAL (
            SELECT id, zone_id FROM {SCHEMA}.noeud
            WHERE agglomeration_id = :agg AND role = 'sous_station'
            ORDER BY geom <-> b.g LIMIT 1
        ) n''',
'''        SELECT n.id AS noeud_id, n.zone_id, b.g, b.g33
        FROM _bat b
        CROSS JOIN LATERAL (
            SELECT id, zone_id FROM {SCHEMA}.noeud
            WHERE agglomeration_id = :agg AND role = 'sous_station'
            ORDER BY zone_centroid <-> b.g LIMIT 1
        ) n''')

# --- C. Accroche des bâtiments dans la composante du poste (pas celle de la source).
rep('''    # Les bâtiments sont accrochés au réseau de service (composante du poste
    # source) : le tronc BT reste ainsi connecté et les épis restent courts.
    comp = conn.execute(text(
        f"SELECT component FROM {SCHEMA}.road_component WHERE node = :v"
    ), {"v": src_v}).scalar()
    conn.execute(text("DROP TABLE IF EXISTS _bat_vert"))
    conn.execute(text(f"""
        CREATE TEMP TABLE _bat_vert AS
        SELECT bp.noeud_id, bp.zone_id, bp.g, v.id AS vertex_id
        FROM _bat_poste bp
        CROSS JOIN LATERAL (
            SELECT v.id FROM {SCHEMA}.road_vertex v
            {"JOIN " + SCHEMA + ".road_component c ON c.node = v.id" if comp is not None else ""}
            {"WHERE c.component = :comp" if comp is not None else ""}
            ORDER BY v.geom <-> bp.g LIMIT 1) v
    """), {"agg": aid, "comp": comp})''',
'''    # Les bâtiments sont accrochés au réseau de la composante de leur poste :
    # chaque tronc BT reste ainsi connecté à son poste.
    conn.execute(text("DROP TABLE IF EXISTS _bat_vert"))
    conn.execute(text(f"""
        CREATE TEMP TABLE _bat_vert AS
        SELECT bp.noeud_id, bp.zone_id, bp.g, v.id AS vertex_id
        FROM _bat_poste bp
        JOIN {SCHEMA}.noeud n ON n.id = bp.noeud_id
        LEFT JOIN {SCHEMA}.road_component pc ON pc.node = n.vertex_id
        CROSS JOIN LATERAL (
            SELECT v.id FROM {SCHEMA}.road_vertex v
            LEFT JOIN {SCHEMA}.road_component c ON c.node = v.id
            WHERE pc.component IS NULL OR c.component = pc.component
            ORDER BY v.geom <-> bp.g LIMIT 1) v
    """), {"agg": aid})''')

# --- D. Épis : repli sur le point du poste quand le tronc BT est vide.
rep('''            SELECT bp.noeud_id, bp.zone_id,
                   ST_Distance(bp.g::geography, l.geom::geography) AS d,
                   ST_MakeLine(ST_ClosestPoint(l.geom, bp.g), bp.g) AS epi
            FROM _bat_vert bp
            JOIN {SCHEMA}.ligne l
              ON l.agglomeration_id = :agg AND l.type_ligne = 'BT'
             AND l.noeud_depart = bp.noeud_id''',
'''            SELECT bp.noeud_id, bp.zone_id,
                   ST_Distance(bp.g::geography,
                               COALESCE(l.geom, n.geom)::geography) AS d,
                   ST_MakeLine(
                       ST_ClosestPoint(COALESCE(l.geom, n.geom), bp.g),
                       bp.g) AS epi
            FROM _bat_vert bp
            JOIN {SCHEMA}.noeud n ON n.id = bp.noeud_id
            LEFT JOIN {SCHEMA}.ligne l
              ON l.agglomeration_id = :agg AND l.type_ligne = 'BT'
             AND l.noeud_depart = bp.noeud_id''')

# --- E. Index KNN sur le centroïde de groupe.
rep('''    conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_noeud_role "
                      f"ON {SCHEMA}.noeud (agglomeration_id, role)"))''',
'''    conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_noeud_role "
                      f"ON {SCHEMA}.noeud (agglomeration_id, role)"))
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_noeud_centroid "
                      f"ON {SCHEMA}.noeud USING gist (zone_centroid)"))''')

io.open(p, 'w', encoding='utf-8', newline='').write(s)
print("OK")
