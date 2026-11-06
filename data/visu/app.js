/* Front Leaflet du plan de distribution électrique (Kwilu). */
(function () {
  "use strict";

  var D = window.DATA || {};
  var FEAT = {
    perimetre: (D.perimetre && D.perimetre.features) || [],
    zone: (D.zone && D.zone.features) || [],
    noeud: (D.noeud && D.noeud.features) || [],
    ligne: (D.ligne && D.ligne.features) || [],
    derive: (D.derive && D.derive.features) || []
  };

  /* ------------------------------------------------------------ couleurs */

  var RAMPE = ["#ffffb2", "#fecc5c", "#fd8d3c", "#f03b20", "#bd0026"];
  var STATUTS = {
    routable: {couleur: "#1f77b4", libelle: "Routable", trait: "solid"},
    routage_proche: {couleur: "#ff7f0e", libelle: "Routage proche", trait: "solid"},
    liaison_directe: {couleur: "#d62728", libelle: "Liaison à construire", trait: "dash"},
    non_routable: {couleur: "#888888", libelle: "Non routable", trait: "dash"}
  };

  var fmt = function (v, d) {
    if (v === null || v === undefined) return d === undefined ? "—" : d;
    return Number(v).toLocaleString("fr-FR");
  };
  var km = function (m) {
    if (m === null || m === undefined) return "—";
    return (Number(m) / 1000).toLocaleString("fr-FR", {maximumFractionDigits: 2}) + " km";
  };
  var m2 = function (v) {
    if (v === null || v === undefined) return "—";
    return fmt(v) + " m²";
  };

  /* ------------------------------------------------------- statistiques */

  var aggs = {};
  FEAT.perimetre.forEach(function (f) {
    var p = f.properties;
    aggs[p.agglomeration_id] = {
      id: p.agglomeration_id,
      rang: p.rang,
      nom: p.nom || ("Agglomération " + p.agglomeration_id),
      population: p.population,
      superficie: p.superficie,
      zones: 0,
      batiments: 0,
      postes: 0,
      jonctions: 0,
      menages: 0,
      mt: 0,
      bt: 0,
      epis: 0,
      lignes: 0,
      routables: 0,
      directes: 0,
      kmMT: 0,
      kmBT: 0,
      kmEpi: 0,
      lineaire: 0,
      noeudSource: null,
      bounds: null
    };
  });

  FEAT.zone.forEach(function (f) {
    var a = aggs[f.properties.agglomeration_id];
    if (!a) return;
    a.zones++;
    a.batiments += f.properties.nb_batiments || 0;
  });

  var postesParZone = {};
  FEAT.noeud.forEach(function (f) {
    var p = f.properties;
    var a = aggs[p.agglomeration_id];
    if (!a) return;
    if (p.role === "source") {
      a.noeudSource = p.id;
    } else if (p.role === "jonction") {
      a.jonctions++;
    } else {
      a.postes++;
      a.menages += p.n_menages || 0;
      var k = p.agglomeration_id + "-" + p.zone_id;
      var z = postesParZone[k] || (postesParZone[k] = {postes: 0, menages: 0});
      z.postes++;
      z.menages += p.n_menages || 0;
    }
  });

  FEAT.ligne.forEach(function (f) {
    var p = f.properties;
    var a = aggs[p.agglomeration_id];
    if (!a) return;
    a.lignes++;
    if (p.type_ligne === "BT") {
      a.bt++;
      a.kmBT += p.longueur_m || 0;
    } else {
      a.mt++;
      a.kmMT += p.longueur_m || 0;
    }
    if (p.statut === "routable") a.routables++;
    if (p.statut === "liaison_directe" || p.statut === "non_routable") a.directes++;
  });

  FEAT.derive.forEach(function (f) {
    var a = aggs[f.properties.agglomeration_id];
    if (!a) return;
    a.epis++;
    a.kmEpi += f.properties.longueur_m || 0;
  });

  var TOTAUX = {
    zones: 0, batiments: 0, postes: 0, jonctions: 0, menages: 0, mt: 0, bt: 0,
    epis: 0, lignes: 0, routables: 0, directes: 0,
    kmMT: 0, kmBT: 0, kmEpi: 0, lineaire: 0
  };
  Object.keys(aggs).forEach(function (k) {
    var a = aggs[k];
    a.lineaire = a.kmMT + a.kmBT + a.kmEpi;
    TOTAUX.zones += a.zones;
    TOTAUX.batiments += a.batiments;
    TOTAUX.postes += a.postes;
    TOTAUX.jonctions += a.jonctions;
    TOTAUX.menages += a.menages;
    TOTAUX.mt += a.mt;
    TOTAUX.bt += a.bt;
    TOTAUX.epis += a.epis;
    TOTAUX.lignes += a.lignes;
    TOTAUX.routables += a.routables;
    TOTAUX.directes += a.directes;
    TOTAUX.kmMT += a.kmMT;
    TOTAUX.kmBT += a.kmBT;
    TOTAUX.kmEpi += a.kmEpi;
    TOTAUX.lineaire += a.lineaire;
  });

  /* ------------------------------------------------- échelle des zones */

  var batiments = FEAT.zone
    .map(function (f) { return f.properties.nb_batiments || 0; })
    .filter(function (v) { return v > 0; })
    .sort(function (a, b) { return a - b; });

  var quantiles = [];
  if (batiments.length) {
    for (var i = 0; i < RAMPE.length; i++) {
      var pos = (batiments.length - 1) * (i / (RAMPE.length - 1));
      var lo = Math.floor(pos), hi = Math.ceil(pos);
      quantiles.push(Math.round(batiments[lo] + (batiments[hi] - batiments[lo]) * (pos - lo)));
    }
  }

  function couleurZone(n) {
    if (!n || n <= 0) return "#d9d9d9";
    for (var i = quantiles.length - 1; i >= 0; i--) {
      if (n >= quantiles[i]) return RAMPE[i];
    }
    return RAMPE[0];
  }

  /* ------------------------------------------------------------- carte */

  var map = L.map("carte", {preferCanvas: true, zoomControl: true}).setView([-5.05, 18.85], 8);

  var fond = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  });
  fond.addTo(map);

  L.control.scale({imperial: false, metric: true}).addTo(map);

  var selectionAgg = null;
  var survolNoeud = null;

  function dimme(aid) {
    return selectionAgg !== null && aid !== selectionAgg;
  }

  /* --------------------------------------------------------- périmètres */

  var couchePerimetre = L.geoJSON(D.perimetre, {
    style: function (f) {
      var actif = selectionAgg === null || selectionAgg === f.properties.agglomeration_id;
      return {
        color: "#12303f",
        weight: actif ? 2 : 1,
        opacity: actif ? 0.85 : 0.25,
        fill: false
      };
    },
    onEachFeature: function (f, layer) {
      var p = f.properties;
      var a = aggs[p.agglomeration_id];
      if (p.centroid && a) {
        layer.bindTooltip(
          '<b>' + a.nom + '</b><br>' + fmt(p.population) + " hab. — " +
          a.zones + " zones, " + a.postes + " sous-stations",
          {permanent: true, direction: "center", className: "etiquette", opacity: 1}
        );
      }
      layer.on("mouseover", function () {
        if (selectionAgg === null || selectionAgg === p.agglomeration_id) layer.setStyle({weight: 3});
      });
      layer.on("mouseout", function () {
        couchePerimetre.resetStyle(layer);
      });
    }
  });

  /* ------------------------------------------------------------- zones */

  var coucheZone = L.geoJSON(D.zone, {
    style: function (f) {
      var p = f.properties;
      return {
        color: "#5b3a00",
        weight: 0.6,
        opacity: dimme(p.agglomeration_id) ? 0.12 : 0.5,
        fillColor: couleurZone(p.nb_batiments),
        fillOpacity: dimme(p.agglomeration_id) ? 0.07 : 0.42
      };
    },
    onEachFeature: function (f, layer) {
      var p = f.properties;
      var ss = postesParZone[p.agglomeration_id + "-" + p.zone_id];
      var html =
        "<b>Zone " + p.zone_id + "</b><br>" +
        "<table>" +
        "<tr><td class='k'>Agglomération</td><td>" +
          (aggs[p.agglomeration_id] ? aggs[p.agglomeration_id].nom : p.agglomeration_id) +
        "</td></tr>" +
        "<tr><td class='k'>Méthode</td><td>" + (p.methode || "—") + "</td></tr>" +
        "<tr><td class='k'>Bâtiments</td><td>" + fmt(p.nb_batiments) + "</td></tr>" +
        "<tr><td class='k'>Surface bâtie</td><td>" + m2(p.surface_batie_m2) + "</td></tr>" +
        (ss ? "<tr><td class='k'>Postes HTA/BT</td><td>" + ss.postes +
              " — " + fmt(ss.menages) + " mén.</td></tr>" : "") +
        "</table>";
      layer.bindPopup(html);
      layer.bindTooltip("Zone " + p.zone_id + " — " + fmt(p.nb_batiments) + " bât.",
        {sticky: true});
      layer.on("mouseover", function () {
        if (!dimme(p.agglomeration_id)) layer.setStyle({weight: 2, fillOpacity: 0.6});
      });
      layer.on("mouseout", function () {
        coucheZone.resetStyle(layer);
      });
    }
  });

  /* ------------------------------------------------------------ lignes */

  function styleLigne(f, etat) {
    var p = f.properties;
    var bt = p.type_ligne === "BT";
    var s = STATUTS[p.statut] || {couleur: "#666", trait: "solid"};
    var actif = !dimme(p.agglomeration_id);
    var couleur = bt ? "#e0a03a" : s.couleur;
    var base = bt ? 1.3 : 2.6;
    var largeur = etat === "survol" ? base + 2 : base;
    if (etat === "liaison") { couleur = "#ffd166"; largeur = base + 2; }
    return {
      color: couleur,
      weight: largeur,
      opacity: etat === "survol" ? 1 : (actif ? 0.92 : 0.08),
      dashArray: s.trait === "dash" ? (bt ? "4 4" : "7 6") : null,
      lineCap: "round"
    };
  }

  var coucheLigne = L.geoJSON(D.ligne, {
    style: function (f) { return styleLigne(f, "normal"); },
    onEachFeature: function (f, layer) {
      var p = f.properties;
      var s = STATUTS[p.statut] || {libelle: p.statut};
      var depart = p.noeud_depart;
      var titre = p.type_ligne === "BT" ? "Tronc BT" : "Tronçon MT";
      var html =
        "<b>" + titre + "</b><br>" +
        "<table>" +
        "<tr><td class='k'>Statut</td><td>" + s.libelle + "</td></tr>" +
        "<tr><td class='k'>Tension</td><td>" + fmt(p.tension_kv) + " kV (" + p.type_ligne + ")</td></tr>" +
        "<tr><td class='k'>Longueur</td><td>" + km(p.longueur_m) + "</td></tr>" +
        "<tr><td class='k'>Tronçons</td><td>" + fmt(p.nb_aretes) + "</td></tr>" +
        (p.type_ligne === "MT" && p.feeder_id ?
          "<tr><td class='k'>Départ (feeder)</td><td>" + p.feeder_id + "</td></tr>" : "") +
        (p.type_ligne === "MT" && p.troncon_id ?
          "<tr><td class='k'>Tronçon</td><td>" + p.troncon_id + "</td></tr>" : "") +
        (p.n_menages !== null && p.n_menages !== undefined ?
          "<tr><td class='k'>Ménages aval</td><td>" + fmt(p.n_menages) + "</td></tr>" : "") +
        (p.detour_ratio ? "<tr><td class='k'>Détour</td><td>×" +
          Number(p.detour_ratio).toLocaleString("fr-FR", {maximumFractionDigits: 1}) + "</td></tr>" : "") +
        (p.longueur_refusee_m ? "<tr><td class='k'>Détour refusé</td><td>" +
          km(p.longueur_refusee_m) + "</td></tr>" : "") +
        "<tr><td class='k'>Agglomération</td><td>" +
          (aggs[p.agglomeration_id] ? aggs[p.agglomeration_id].nom : p.agglomeration_id) +
        "</td></tr>" +
        "</table>";
      layer.bindPopup(html);
      layer.bindTooltip(km(p.longueur_m) + " — " + s.libelle, {sticky: true});
      layer.on("mouseover", function () { layer.setStyle(styleLigne(f, "survol")); });
      layer.on("mouseout", function () { layer.setStyle(styleLigne(f, "normal")); });
    }
  });

  /* ------------------------------------------------------------ postes */

  var coucheSource = L.geoJSON(
    {type: "FeatureCollection", features: FEAT.noeud.filter(function (f) {
      return f.properties.role === "source";
    })},
    {
      pointToLayer: function (f, latlng) {
        return L.circleMarker(latlng, {
          radius: 9,
          color: "#0d1218",
          weight: 2,
          fillColor: "#ffd166",
          fillOpacity: 1
        });
      },
      onEachFeature: function (f, layer) {
        var p = f.properties;
        var html =
          "<b>Poste source HTB</b><br>" +
          "<table>" +
          "<tr><td class='k'>Agglomération</td><td>" +
            (aggs[p.agglomeration_id] ? aggs[p.agglomeration_id].nom : p.agglomeration_id) + "</td></tr>" +
          "<tr><td class='k'>Tension</td><td>" + fmt(p.tension_kv) + " kV</td></tr>" +
          "<tr><td class='k'>Accroche réseau</td><td>" + fmt(p.snap_distance_m, 0) + " m</td></tr>" +
          "<tr><td class='k'>Lignes départ</td><td>" +
            (aggs[p.agglomeration_id] ? aggs[p.agglomeration_id].lignes : "—") + "</td></tr>" +
          "</table>";
        layer.bindPopup(html);
        layer.bindTooltip("Poste source " + fmt(p.tension_kv) + " kV", {sticky: true});
        layer.on("mouseover", function () {
          survolNoeud = p.id;
          surlignerLignes(p.id);
        });
        layer.on("mouseout", function () {
          survolNoeud = null;
          surlignerLignes(null);
        });
      }
    }
  );

  var coucheSousStation = L.geoJSON(
    {type: "FeatureCollection", features: FEAT.noeud.filter(function (f) {
      return f.properties.role === "sous_station";
    })},
    {
      pointToLayer: function (f, latlng) {
        var n = f.properties.nb_batiments || 0;
        return L.circleMarker(latlng, {
          radius: Math.max(3, Math.min(7, 2 + Math.sqrt(n) / 2.5)),
          color: "#0d1218",
          weight: 1.2,
          fillColor: "#43c59e",
          fillOpacity: 0.95
        });
      },
      onEachFeature: function (f, layer) {
        var p = f.properties;
        var html =
          "<b>Sous-station MT</b><br>" +
          "<table>" +
          "<tr><td class='k'>Zone</td><td>" + p.zone_id + "</td></tr>" +
          "<tr><td class='k'>Tension</td><td>" + fmt(p.tension_kv) + " kV</td></tr>" +
          "<tr><td class='k'>Bâtiments desservis</td><td>" + fmt(p.nb_batiments) + "</td></tr>" +
          "<tr><td class='k'>Surface bâtie</td><td>" + m2(p.surface_batie_m2) + "</td></tr>" +
          "<tr><td class='k'>Accroche réseau</td><td>" + fmt(p.snap_distance_m, 0) + " m</td></tr>" +
          "</table>";
        layer.bindPopup(html);
        layer.bindTooltip("Sous-station — " + fmt(p.nb_batiments) + " bât.", {sticky: true});
        layer.on("mouseover", function () {
          layer.setStyle({radius: layer.getRadius() + 2, weight: 2.5});
        });
        layer.on("mouseout", function () {
          layer.setStyle({radius: Math.max(3, Math.min(7,
            2 + Math.sqrt(p.nb_batiments || 0) / 2.5)), weight: 1.2});
        });
      }
    }
  );

  var coucheJonction = L.geoJSON(
    {type: "FeatureCollection", features: FEAT.noeud.filter(function (f) {
      return f.properties.role === "jonction";
    })},
    {
      pointToLayer: function (f, latlng) {
        return L.circleMarker(latlng, {
          radius: 3.4, color: "#1a1205", weight: 1,
          fillColor: "#f4c95d", fillOpacity: 1
        });
      },
      onEachFeature: function (f, layer) {
        var p = f.properties;
        layer.bindTooltip("Jonction MT", {sticky: true});
        layer.bindPopup(
          "<b>Jonction MT</b><br>" +
          "<table>" +
          "<tr><td class='k'>Tension</td><td>" + fmt(p.tension_kv) + " kV</td></tr>" +
          "<tr><td class='k'>Nœud amont</td><td>" + (p.parent_noeud_id || "—") + "</td></tr>" +
          "<tr><td class='k'>Agglomération</td><td>" +
            (aggs[p.agglomeration_id] ? aggs[p.agglomeration_id].nom : p.agglomeration_id) +
          "</td></tr>" +
          "</table>");
      }
    }
  );

  var coucheDerive = L.geoJSON(
    {type: "FeatureCollection", features: FEAT.derive},
    {
      style: function (f) {
        var actif = !dimme(f.properties.agglomeration_id);
        return {color: "#7ad3b3", weight: 1, opacity: actif ? 0.75 : 0.05};
      },
      onEachFeature: function (f, layer) {
        var p = f.properties;
        layer.bindTooltip("Épi BT — " + km(p.longueur_m), {sticky: true});
        layer.bindPopup(
          "<b>Épi BT</b><br>" +
          "<table>" +
          "<tr><td class='k'>Longueur</td><td>" + km(p.longueur_m) + "</td></tr>" +
          "<tr><td class='k'>Écart au tronc</td><td>" + km(p.d_tronc_m) + "</td></tr>" +
          "<tr><td class='k'>Poste</td><td>" + (p.noeud_id || "—") + "</td></tr>" +
          "</table>");
      }
    }
  );

  function surlignerLignes(noeudId) {
    coucheLigne.eachLayer(function (layer) {
      var p = layer.feature && layer.feature.properties;
      if (!p) return;
      if (noeudId === null) {
        layer.setStyle(styleLigne(layer.feature, "normal"));
      } else if (p.noeud_depart === noeudId) {
        layer.setStyle(styleLigne(layer.feature, "liaison"));
        layer.bringToFront();
      }
    });
  }

  /* ------------------------------------------------------ groupes de calques */

  var groupeLignes = L.layerGroup([coucheLigne]);
  var groupeEpis = L.layerGroup([coucheDerive]);
  var groupeZones = L.layerGroup([coucheZone]);
  var groupePostes = L.layerGroup([coucheSousStation, coucheJonction, coucheSource]);
  var groupePerimetre = L.layerGroup([couchePerimetre]);

  L.control.layers(
    {
      "Postes (source, HTA, jonctions)": groupePostes,
      "Réseau MT + troncs BT": groupeLignes,
      "Épis BT": groupeEpis,
      "Zones de distribution": groupeZones,
      "Périmètres": groupePerimetre
    },
    {"Fond OpenStreetMap": fond},
    {position: "topright", collapsed: true}
  ).addTo(map);

  groupeLignes.addTo(map);
  groupeEpis.addTo(map);
  groupeZones.addTo(map);
  groupePostes.addTo(map);
  groupePerimetre.addTo(map);

  /* ------------------------------------------------------------- panneau */

  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }

  var panneau = document.getElementById("panneau");

  var kpi = el("div", "kpi");
  kpi.innerHTML =
    "<div><span>Agglomérations</span><b>" + Object.keys(aggs).length + "</b></div>" +
    "<div><span>Zones</span><b>" + fmt(TOTAUX.zones) + "</b></div>" +
    "<div><span>Postes HTA/BT</span><b>" + fmt(TOTAUX.postes) + "</b></div>" +
    "<div><span>Jonctions MT</span><b>" + fmt(TOTAUX.jonctions) + "</b></div>" +
    "<div><span>Ménages</span><b>" + fmt(TOTAUX.menages) + "</b></div>" +
    "<div><span>Épis BT</span><b>" + fmt(TOTAUX.epis) + "</b></div>" +
    "<div><span>Linéaire MT</span><b>" + km(TOTAUX.kmMT) + "</b></div>" +
    "<div><span>Linéaire BT</span><b>" + km(TOTAUX.kmBT + TOTAUX.kmEpi) + "</b></div>";
  panneau.appendChild(el("div", "bloc")).appendChild(kpi);

  /* liste des agglomérations */
  var blocAgg = el("div", "bloc");
  blocAgg.appendChild(el("h2", null, "Agglomérations (cliquer pour isoler)"));
  var listeAgg = el("div");
  blocAgg.appendChild(listeAgg);

  var ordreAgg = Object.keys(aggs).map(function (k) { return aggs[k]; })
    .sort(function (a, b) { return a.rang - b.rang; });

  var lignesAgg = {};
  ordreAgg.forEach(function (a) {
    var b = el("button", "agg");
    b.innerHTML =
      "<span class='rang'>" + a.rang + "</span>" +
      "<span class='pastille' style='background:" + couleurZone(a.batiments / Math.max(a.zones, 1)) + "'></span>" +
      "<span class='nom'>" + a.nom + "</span>" +
      "<span class='pop'>" + fmt(a.population) + "</span>";
    b.addEventListener("click", function () { selectionner(a.id); });
    listeAgg.appendChild(b);
    lignesAgg[a.id] = b;
  });
  panneau.appendChild(blocAgg);

  /* tableau de synthèse */
  var blocTab = el("div", "bloc");
  blocTab.appendChild(el("h2", null, "Synthèse"));
  var table = el("table", "recap");
  table.innerHTML =
    "<thead><tr><th>Agglomération</th><th>Postes</th><th>MT</th>" +
    "<th>BT</th><th>Épis</th><th>Linéaire</th></tr></thead>";
  var tbody = el("tbody");
  var lignesTab = {};
  ordreAgg.forEach(function (a) {
    var tr = el("tr");
    tr.innerHTML =
      "<td>" + a.nom + "</td>" +
      "<td>" + a.postes + "</td>" +
      "<td>" + a.mt + "</td>" +
      "<td>" + a.bt + "</td>" +
      "<td>" + a.epis + "</td>" +
      "<td>" + km(a.lineaire) + "</td>";
    tr.addEventListener("click", function () { selectionner(a.id); });
    tbody.appendChild(tr);
    lignesTab[a.id] = tr;
  });
  table.appendChild(tbody);
  blocTab.appendChild(table);
  panneau.appendChild(blocTab);

  /* légende */
  var blocLeg = el("div", "bloc");
  blocLeg.appendChild(el("h2", null, "Légende"));
  var legende = el("div", "legende");
  legende.innerHTML =
    "<div class='item'><span class='trait' style='border-top:3px solid #1f77b4'></span>Tronçon MT routable</div>" +
    "<div class='item'><span class='trait' style='border-top:3px dashed #d62728'></span>Tronçon MT à construire</div>" +
    "<div class='item'><span class='trait' style='border-top:2px solid #e0a03a'></span>Tronc BT</div>" +
    "<div class='item'><span class='trait' style='border-top:1px solid #7ad3b3'></span>Épi BT</div>" +
    "<div class='item'><span class='pastille' style='background:#ffd166'></span>Poste source HTB</div>" +
    "<div class='item'><span class='pastille' style='background:#43c59e'></span>Poste HTA/BT</div>" +
    "<div class='item'><span class='pastille' style='background:#f4c95d;width:8px;height:8px'></span>Jonction MT</div>";
  blocLeg.appendChild(legende);
  var echelle = el("div");
  echelle.style.marginTop = "10px";
  echelle.innerHTML =
    "<div class='degrade'></div>" +
    "<div class='echelle'><span>0</span><span>" +
      (quantiles.length ? fmt(quantiles[Math.floor(quantiles.length / 2)]) : "—") +
    "</span><span>" + (quantiles.length ? fmt(quantiles[quantiles.length - 1]) : "—") + "</span></div>" +
    "<div class='note'>Bâtiments desservis par zone</div>";
  blocLeg.appendChild(echelle);
  panneau.appendChild(blocLeg);

  /* boutons */
  var blocBt = el("div", "bloc");
  blocBt.appendChild(el("h2", null, "Affichage"));
  var boutons = el("div", "boutons");
  var bTout = el("button", "action on", "Tout afficher");
  var bSansFond = el("button", "action", "Sans fond");
  var bPostes = el("button", "action on", "Postes");
  var bLignes = el("button", "action on", "MT/BT");
  var bEpis = el("button", "action on", "Épis");
  var bZones = el("button", "action on", "Zones");
  var bPerim = el("button", "action on", "Périmètres");
  [bTout, bSansFond, bPostes, bLignes, bEpis, bZones, bPerim].forEach(function (b) {
    boutons.appendChild(b);
  });
  blocBt.appendChild(boutons);
  blocBt.appendChild(el("div", "note",
    "Fond : tuiles OpenStreetMap (bouton « Sans fond » pour un affichage hors-ligne). " +
    "Clic droit ou molette pour zoomer ; les infobulles donnent le détail de chaque objet."));
  panneau.appendChild(blocBt);

  /* ------------------------------------------------------- interactions */

  function rafraichirStyles() {
    coucheZone.eachLayer(function (layer) {
      coucheZone.resetStyle(layer);
    });
    coucheLigne.eachLayer(function (layer) {
      layer.setStyle(styleLigne(layer.feature, "normal"));
    });
    couchePerimetre.eachLayer(function (layer) {
      couchePerimetre.resetStyle(layer);
    });
    Object.keys(lignesAgg).forEach(function (k) {
      lignesAgg[k].classList.toggle("actif", String(selectionAgg) === k);
      lignesTab[k].classList.toggle("actif", String(selectionAgg) === k);
    });
  }

  function selectionner(aggId) {
    selectionAgg = (selectionAgg === aggId) ? null : aggId;
    rafraichirStyles();
    if (selectionAgg === null) {
      map.fitBounds(boundsGlobal, {padding: [18, 18]});
      bTout.classList.add("on");
    } else {
      var a = aggs[selectionAgg];
      var couche = L.geoJSON(
        {type: "FeatureCollection", features: FEAT.perimetre.filter(function (f) {
          return f.properties.agglomeration_id === selectionAgg;
        })},
        {style: {color: "#12303f", weight: 2, fill: false, fillOpacity: 0}}
      );
      var b = couche.getBounds();
      if (b.isValid()) map.fitBounds(b, {padding: [40, 40]});
      bTout.classList.remove("on");
    }
  }

  bTout.addEventListener("click", function () { selectionAgg = null; rafraichirStyles(); map.fitBounds(boundsGlobal, {padding: [18, 18]}); });
  bSansFond.addEventListener("click", function () {
    if (map.hasLayer(fond)) {
      map.removeLayer(fond);
      bSansFond.classList.add("on");
      bSansFond.textContent = "Fond OSM";
    } else {
      fond.addTo(map);
      bSansFond.classList.remove("on");
      bSansFond.textContent = "Sans fond";
    }
  });
  function bascule(couche, bouton) {
    return function () {
      if (map.hasLayer(couche)) {
        map.removeLayer(couche);
        bouton.classList.remove("on");
      } else {
        couche.addTo(map);
        bouton.classList.add("on");
      }
    };
  }
  bPostes.addEventListener("click", bascule(groupePostes, bPostes));
  bLignes.addEventListener("click", bascule(groupeLignes, bLignes));
  bEpis.addEventListener("click", bascule(groupeEpis, bEpis));
  bZones.addEventListener("click", bascule(groupeZones, bZones));
  bPerim.addEventListener("click", bascule(groupePerimetre, bPerim));

  var boundsGlobal = L.latLngBounds([]);
  FEAT.perimetre.forEach(function (f) {
    var c = f.properties.centroid;
    if (c && c.coordinates) {
      boundsGlobal.extend([c.coordinates[1], c.coordinates[0]]);
    }
  });
  if (!boundsGlobal.isValid()) {
    FEAT.perimetre.forEach(function (f) {
      if (f.geometry && f.geometry.coordinates) {
        L.geoJSON(f).getBounds && boundsGlobal.extend(L.geoJSON(f).getBounds());
      }
    });
  }
  if (boundsGlobal.isValid()) map.fitBounds(boundsGlobal, {padding: [18, 18]});

  /* Poignée de débogage / scripting (console navigateur) */
  window.visu = {
    map: map,
    couches: {
      perimetre: couchePerimetre, zone: coucheZone,
      ligne: coucheLigne, source: coucheSource, sousStation: coucheSousStation,
      jonction: coucheJonction, derive: coucheDerive
    },
    groupes: {perimetre: groupePerimetre, zone: groupeZones, ligne: groupeLignes,
              epis: groupeEpis, postes: groupePostes},
    stats: {aggs: aggs, totaux: TOTAUX},
    selectionner: selectionner
  };
})();
