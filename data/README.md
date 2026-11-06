# window ip
```bash

ip route show | grep default | awk '{print $3}'

```

# DB conf postgres
1. Sur votre machine Windows, ouvrez l'explorateur de fichiers.

2. Allez dans le dossier data de PostgreSQL. Par défaut :
`C:\Program Files\PostgreSQL\<version>\data\pg_hba.conf`

(Ou via pgAdmin : clic droit sur votre serveur > Configuration > Edit pg_hba.conf)

3. Ouvrez pg_hba.conf avec Bloc-notes en tant qu'administrateur.

4. Faites défiler le fichier jusqu'à la fin (dans la section # IPv4 local connections:) et ajoutez cette ligne :

`host    anser_route     postgres        172.21.0.0/16           trust`
# Changer le type osm_id

```sql
-- 1. Convertir la colonne osm_id de text vers bigint
ALTER TABLE gis_osm_roads_free 
  ALTER COLUMN osm_id TYPE BIGINT USING osm_id::bigint;

-- 2. Supprimer la table si elle a échoué précédemment
DROP TABLE IF EXISTS gis_osm_roads_free_vertices_pgr;
```

# Création de la table des nœuds (intersections)

```sql

-- Création de la table des nœuds (intersections)
CREATE TABLE gis_osm_roads_free_vertices_pgr AS
SELECT 
    id,
    geom AS the_geom,
    in_edge,
    out_edge
FROM pgr_extractVertices(
    'SELECT id, geometry AS geom FROM gis_osm_roads_free'
);

-- Index spatial indispensable sur les nœuds
CREATE INDEX ON gis_osm_roads_free_vertices_pgr USING gist(the_geom);

-- 1. S'assurer que les colonnes existent
ALTER TABLE gis_osm_roads_free ADD COLUMN IF NOT EXISTS source BIGINT;
ALTER TABLE gis_osm_roads_free ADD COLUMN IF NOT EXISTS target BIGINT;

-- 2. Mettre à jour le nœud de départ (source)
UPDATE gis_osm_roads_free r
SET source = v.id
FROM gis_osm_roads_free_vertices_pgr v
WHERE ST_DWithin(ST_StartPoint(r.geometry), v.the_geom, 0.0001);

-- 3. Mettre à jour le nœud d'arrivée (target)
UPDATE gis_osm_roads_free r
SET target = v.id
FROM gis_osm_roads_free_vertices_pgr v
WHERE ST_DWithin(ST_EndPoint(r.geometry), v.the_geom, 0.0001);

CREATE INDEX IF NOT EXISTS idx_roads_source ON gis_osm_roads_free(source);
CREATE INDEX IF NOT EXISTS idx_roads_target ON gis_osm_roads_free(target);
CREATE INDEX IF NOT EXISTS idx_components_node ON gis_osm_roads_components(node);

```

> Note : avec pgRouting ≥ 4, `pgr_createTopology` et `pgr_extractVertices` ont été
> retirés ou modifiés (la colonne doit s'appeler `geom`). Le script
> `plan_electricite.py` construit lui-même sa topologie (fusion des sommets par
> grille) et ne dépend d'aucune de ces fonctions.

# Plan de distribution électrique

`plan_electricite.py` produit un **schéma de distribution électrique** pour les
plus grandes agglomérations du Kwilu (puis n'importe quelle agglomération du
pays) : zones de distribution, poste source HTB, **réseau MT arborescent**,
**postes HTA/BT** et **desserte BT** routée sur la voirie OSM. Le script est
autonome : il ne modifie ni `etl.py` ni les tables applicatives, il écrit
uniquement dans le schéma `plan`.

## Modèle de réseau

* **Zones** : DBSCAN sur les bâtiments OSM, puis raffinement KMeans des zones
  trop denses (`--max-bat-per-zone`).
* **Postes HTA/BT** : chaque zone est découpée en sous-groupes d'au plus
  `--max-menages-poste` ménages (1 bâtiment OSM ≈ 1 ménage, ratio de sécurité
  `POSTE_SPLIT_RATIO`). Chaque sous-groupe devient un poste, accroché au sommet
  routier le plus proche **de la composante de service** (distance ≤
  `--max-snap-m`, sinon le poste garde son propre sommet). Plusieurs postes
  peuvent partager un même sommet routier : le MT est alors mutualisé, chaque
  poste conservant son propre tronc BT. Les bâtiments sont affectés au poste le
  plus proche de leur **centroïde de zone** (charge équilibrée) tout en restant
  dessinés sur la voirie.
* **Réseau MT** : arbre de plus courts chemins (Dijkstra multi-cibles) depuis le
  poste source vers tous les postes. Les branches sont coupées aux postes et aux
  **jonctions** (sommets de degré ≥ 3) ; chaque tronçon porte un `troncon_id` et
  un `feeder_id` (départ de premier niveau depuis la source).
* **Desserte BT** : pour chaque poste, arbre de plus courts chemins vers les
  sommets routiers des bâtiments qu'il dessert, stocké en une géométrie
  `MultiLineString` (le « tronc BT »).
* **Épis BT** : bâtiments situés à plus de `--bt-seuil-epi` mètres du tronc
  (raccordement direct, à construire) → table `plan.derive`.
* **Postes hors voirie** : un poste dont le sommet n'est pas joignable depuis la
  source reçoit une liaison MT directe (statut `liaison_directe`).

Les tensions (`--tension-source`, `--tension-mt`, `--tension-bt`) sont
indicatives et servent au dimensionnement / à l'affichage ; aucun calcul
électrique (chutes de tension, courants de court-circuit) n'est réalisé.

## Exécution

```bash
# tout en une fois
python plan_electricite.py plan
# étapes individuelles (rejouables une par une)
python plan_electricite.py agglomerations   # backup -> plan.province/territoire/agglomeration
python plan_electricite.py perimetre        # top 10 par population
python plan_electricite.py roads-graph      # graphe routable (grille de fusion)
python plan_electricite.py cluster          # DBSCAN puis KMeans sur les bâtiments
python plan_electricite.py nodes            # poste source + postes HTA/BT
python plan_electricite.py lines            # arbres MT + BT -> plan.ligne / plan.derive
python plan_electricite.py report --export export_plan.gpkg
```

## Calcul d'une agglomération à la demande

La commande `agg` calcule **une seule agglomération, n'importe laquelle du
pays** (pas seulement le Kwilu) : périmètre, zones de distribution, poste
source, postes HTA/BT, réseau MT et desserte BT.

```bash
python plan_electricite.py agg 32            # Kisangani : calcule si absent
python plan_electricite.py agg --id 32       # même chose (alias)
python plan_electricite.py agg 32 --force    # recalcule tout, même si déjà fait
```

* si le calcul existe déjà (périmètre, zones, poste source, postes, tronçons MT,
  troncs BT et aucun sommet orphelin) : rien n'est écrit, la commande affiche
  `EXISTE : …` ;
* sinon : elle crée le périmètre depuis `plan.agglomeration`, **étend le graphe
  routier** de la zone (sans le reconstruire), puis calcule zones, postes et
  lignes — les autres agglomérations ne sont jamais modifiées ;
* chaque étape est jouée dans sa propre transaction et rejouée automatiquement
  si la connexion tombe (verrou atteint, coupure réseau) ;
* une agglomération inconnue est refusée (code de sortie 1) et la liste des
  agglomérations déjà calculées est rappelée.

Les paramètres techniques du graphe (maille de fusion, marge, classes de
routes retenues) sont mémorisés dans `plan.parametres` : les reconstructions
restent ainsi reproductibles et l'extension d'une agglomération utilise
exactement les mêmes classes que le graphe d'origine.

API Python réutilisable :

```python
import plan_electricite as pe

with pe.get_engine().connect() as conn:
    etat = pe.etat_agglomeration(conn, 32)   # état de l'agglomération
res = pe.calculer_agglomeration(32)          # calcule si nécessaire
res = pe.calculer_agglomeration(32, force=True)
```

### Mode ciblé `--only` (non destructif)

`--only <ids…>` limite le traitement à certaines agglomérations **sans toucher
aux autres** : les tables `zone`, `noeud`, `ligne`, `derive` et `perimetre` sont
conservées, seules les lignes de l'agglomération demandée sont remplacées.

```bash
python plan_electricite.py perimetre --only 22 32   # ces périmètres seuls
python plan_electricite.py cluster  --only 22       # ces zones seules
python plan_electricite.py nodes    --only 22       # ces postes seuls
python plan_electricite.py lines    --only 22       # ces lignes seules
```

Sans `--only`, le comportement historique est conservé (les tables sont
reconstruites entièrement).

Options principales :

| Option | Défaut | Rôle |
|---|---|---|
| `--top` | 10 | nombre d'agglomérations retenues |
| `--snap-tolerance` | 150 | fusion des sommets routiers (m) |
| `--road-classes` | mémorisées | routes retenues pour le tracé (`""` = toutes) |
| `--eps` / `--minpoints` | 200 / 10 | paramètres DBSCAN |
| `--max-bat-per-zone` | 500 | seuil avant raffinement KMeans |
| `--max-menages-poste` | 200 | plafond de ménages desservis par poste |
| `--max-snap-m` | 1500 | rayon d'accroche / de relogement d'un poste (m) |
| `--max-troncon-km` | 15 | longueur maximale d'un tronçon MT |
| `--bt-seuil-epi` | 150 | distance au-delà de laquelle un bâtiment reçoit un épi (m) |
| `--tension-source` | 220 | tension du poste source (kV) |
| `--tension-mt` | 30 | tension du réseau MT (kV) |
| `--tension-bt` | 0.4 | tension du réseau BT (kV) |
| `--bt-sur-route` | — | réservé (le BT suit toujours la voirie) |

## Tables produites (schéma `plan`)

`province`, `territoire`, `agglomeration`, `agglomeration_pop`, `perimetre`,
`road`, `road_vertex`, `road_component`, `zone`, `noeud`, `ligne`, `derive`,
`agg_statut`, `parametres` (paramètres techniques du calcul : maille de fusion,
marge, classes routières).

* `plan.noeud.role` : `source`, `sous_station`, `jonction` ;
  `type_poste` : `source`, `poste_hta` ; `parent_noeud_id` donne le nœud amont
  dans l'arbre MT ; `n_menages` le nombre de ménages desservis.
* `plan.ligne.type_ligne` : `MT` (tronçons, `LineString`) ou `BT` (un tronc par
  poste, `MultiLineString`).
* `plan.derive` : épis BT (bâtiments éloignés du tronc), avec `d_tronc_m`.
* `plan.ligne.statut` : `routable` (tracé sur la voirie OSM) ou
  `liaison_directe` (tracé à construire, aucun chemin routier disponible).
* `plan.agg_statut` : synthèse par agglomération (zones, postes, jonctions,
  tronçons MT, troncs BT, épis, km MT, km BT, ménages, date de mise à jour).

## Carte interactive (Leaflet)

`visu_plan.py` exporte le plan vers une carte Leaflet **autonome** : un seul
fichier HTML contenant la bibliothèque, les données GeoJSON et l'interface.
Aucun serveur web n'est nécessaire (double-clic), mais un serveur local est
proposé pour éviter les restrictions du protocole `file://`.

```bash
# génère visu/plan_kwilu.html à partir du schéma plan
./venv/bin/python visu_plan.py export

# export + serveur local sur http://localhost:8000/plan_kwilu.html
./venv/bin/python visu_plan.py serve

# contrôle du fichier généré (JSON inline, couches non vides, autonomie)
./venv/bin/python visu_plan.py check
```

Fonctionnalités : fond de carte OpenStreetMap avec bouton « Sans fond »,
contrôle de calques (source, postes, jonctions, MT, troncs BT, épis), panneau
latéral (agglomérations cliquables pour isoler une ville, tableau de synthèse,
légende), zones colorées selon le nombre de bâtiments desservis, MT en bleu
épais, BT en ambre fin, liaison directe en rouge pointillé, épis en pointillés,
infobulles et fenêtres contextuelles.

Leaflet est mis en cache dans `visu/vendor/` (téléchargé une seule fois, puis
embarqué dans le HTML). La connexion Internet n'est nécessaire que pour les
tuiles de fond OSM ; le reste de la carte fonctionne hors-ligne.

Fichiers : `visu_plan.py`, `visu/template.html`, `visu/app.js`, `visu/style.css`,
`visu/vendor/leaflet.{js,css}`.

## Limites connues

* Le réseau OSM rural est incomplet : les liaisons `liaison_directe` signalent
  des zones sans voirie cartographiée, pas des erreurs du calcul.
* Les arbres BT de deux postes voisins peuvent emprunter les mêmes rues : la
  longueur BT totale additionne donc des tronçons partagés.
* Dans les zones où la voirie est très lâche, les bâtiments s'accrochent au
  même sommet que leur poste : le tronc BT est alors vide et toute la desserte
  passe par des épis (`liaison_directe` côté BT, dénombrement dans `--verbose`).
* Un poste dont le sommet n'est pas joignable depuis la source reçoit une
  liaison MT directe (`liaison_directe`, droite entre le poste et l'arbre MT).
  Ces raccordements vont de 0 à 10 % des postes sur les agglomérations
  urbaines (Kikwit, Bulungu, Bandundu, Kisangani) et jusqu'à ~55 % sur les
  zones rurales très peu cartographiées (Gungu, Idiofa).
* Les sommets sont fusionnés par grille (150 m) : les tracés présentent de
  légers décrochements aux jonctions, et les distances sont celles des roads OSM.
* Aucun calcul électrique (chute de tension, foisonnement, court-circuit) n'est
  effectué : les tensions et le plafond de ménages servent au dimensionnement.
