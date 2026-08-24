"""Génère une page web autonome pour parcourir l'index.

Trois partis pris :

* **Une seule page**, pas un fichier HTML par dossier. L'index compte plus de
  9 000 dossiers ; en générer autant de pages doublerait le nombre de fichiers
  du dépôt pour un résultat équivalent.
* **Arborescence embarquée dans le HTML**, pas chargée par ``fetch``. Un
  fichier JSON séparé serait bloqué par CORS dès qu'on ouvre la page en
  ``file://`` — or pouvoir double-cliquer le fichier sans serveur est
  précisément l'intérêt d'un explorateur statique.
* **Sortie non versionnée** (``site/`` est dans .gitignore). C'est un artefact
  dérivé : un bloc JSON d'environ un mégaoctet réécrit intégralement à chaque
  run est exactement le genre de fichier que ce projet a choisi de ne pas
  mettre dans Git.

Le module s'appelle ``explorer`` et non ``site`` : ce dernier masquerait le
module ``site`` de la bibliothèque standard.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from . import config, fsutil, pathmap
from .store import DirIndex, Store

#: Clés volontairement courtes : répétées des dizaines de milliers de fois,
#: elles pèsent plus lourd que les données elles-mêmes en noms explicites.
KEY_DIRS = "d"
KEY_FILES = "f"
KEY_PAGE = "p"
KEY_TOTAL = "n"


def build_tree(store: Store) -> dict:
    """Arborescence imbriquée, prête à sérialiser.

    Les segments sont *déscapés* : le disque stocke ``%4A%4F_2012`` pour
    préserver la casse, mais l'explorateur doit afficher ``JO_2012`` et
    reconstruire l'URL réelle.
    """
    root: dict = {}
    for relpath, entry in sorted(store.dirs.items()):
        node = root
        for segment in relpath.split("/"):
            node = node.setdefault(KEY_DIRS, {}).setdefault(
                pathmap.unescape_segment(segment), {}
            )
        _fill(node, entry)
    _count(root)
    return root


def _fill(node: dict, entry: DirIndex) -> None:
    if entry.is_page:
        node[KEY_PAGE] = 1 if entry.page_dead else 0
    if entry.slugs:
        node[KEY_FILES] = [
            [slug, 1 if dead else 0] for slug, dead in sorted(entry.slugs.items())
        ]


def _count(node: dict) -> int:
    """Annote chaque nœud du total d'URLs de son sous-arbre.

    Calculé ici plutôt qu'en JavaScript : le navigateur n'aurait aucune raison
    de reparcourir tout l'arbre à chaque affichage de dossier.
    """
    total = len(node.get(KEY_FILES, ())) + (1 if KEY_PAGE in node else 0)
    for child in node.get(KEY_DIRS, {}).values():
        total += _count(child)
    node[KEY_TOTAL] = total
    return total


def _payload(store: Store) -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "stats": store.stats(),
        "tree": build_tree(store),
    }


def render(store: Store) -> str:
    """Retourne le HTML complet de l'explorateur."""
    data = json.dumps(_payload(store), ensure_ascii=False, separators=(",", ":"))
    # `</script>` dans une chaîne JSON refermerait la balise et casserait la
    # page : on neutralise le chevron, que JSON relit comme un `<` ordinaire.
    data = data.replace("<", "\\u003c")
    return _TEMPLATE.replace("__DATA__", data)


def generate(store: Store, output_dir: Path | None = None) -> Path:
    """Écrit la page et retourne son chemin."""
    output_dir = Path(output_dir) if output_dir is not None else config.SITE_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "index.html"
    fsutil.write_text(path, render(store))
    return path


_TEMPLATE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Index des URLs de rts.ch</title>
<style>
  :root {
    --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --line: #e5e7eb;
    --accent: #0f62fe; --dead: #b91c1c; --dead-bg: #fef2f2; --hover: #f3f4f6;
    --chip: #f9fafb;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115; --fg: #e6e6e6; --muted: #9ca3af; --line: #262b33;
      --accent: #6ea8fe; --dead: #f87171; --dead-bg: #2a1515; --hover: #171a21;
      --chip: #161920;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  header {
    padding: 1.25rem 1.5rem; border-bottom: 1px solid var(--line);
    display: flex; flex-wrap: wrap; gap: 1rem; align-items: baseline;
  }
  h1 { font-size: 1.05rem; margin: 0; font-weight: 600; }
  h1 span { color: var(--muted); font-weight: 400; }
  nav { margin-left: auto; display: flex; gap: .5rem; }
  button {
    font: inherit; padding: .35rem .8rem; border: 1px solid var(--line);
    background: var(--chip); color: var(--fg); border-radius: 6px; cursor: pointer;
  }
  button[aria-pressed="true"] { border-color: var(--accent); color: var(--accent); }
  main { padding: 1.25rem 1.5rem; max-width: 1100px; }
  .crumbs { margin-bottom: 1rem; font-size: .9rem; word-break: break-all; }
  .crumbs a { color: var(--accent); cursor: pointer; text-decoration: none; }
  .crumbs a:hover { text-decoration: underline; }
  .crumbs .sep { color: var(--muted); margin: 0 .3rem; }
  .resume {
    display: flex; flex-wrap: wrap; gap: .4rem 1.5rem; margin-bottom: 1rem;
    padding: .6rem 1rem; background: var(--chip); border: 1px solid var(--line);
    border-radius: 8px; font-size: .82rem;
  }
  .resume .item { color: var(--muted); }
  .resume .item b { color: var(--fg); font-variant-numeric: tabular-nums; font-weight: 600; }
  ul { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--line); }
  li {
    display: flex; gap: .6rem; align-items: baseline; padding: .45rem .6rem;
    border-bottom: 1px solid var(--line); word-break: break-all;
  }
  li:hover { background: var(--hover); }
  li.dead { background: var(--dead-bg); }
  li.selected { outline: 2px solid var(--accent); outline-offset: -2px; background: var(--hover); }
  .ico { color: var(--muted); flex: none; width: 1.1rem; text-align: center; }
  .name { flex: 1; }
  .name a { color: var(--accent); text-decoration: none; cursor: pointer; }
  .name a:hover { text-decoration: underline; }
  li.dead .name a { color: var(--dead); text-decoration: line-through; }
  .count { color: var(--muted); font-size: .8rem; flex: none; font-variant-numeric: tabular-nums; }
  .tag {
    flex: none; font-size: .7rem; text-transform: uppercase; letter-spacing: .04em;
    color: var(--dead); border: 1px solid currentColor; border-radius: 4px; padding: 0 .3rem;
  }
  .empty { color: var(--muted); padding: 1rem .6rem; }
  table { border-collapse: collapse; width: 100%; max-width: 560px; }
  th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--line); }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  h2 { font-size: .95rem; margin: 1.75rem 0 .5rem; }
  footer { color: var(--muted); font-size: .8rem; padding: 1.5rem; }
  .overflow { overflow-x: auto; }
</style>
</head>
<body>
<header>
  <h1>Index des URLs de <span>rts.ch</span></h1>
  <nav>
    <button id="tab-browse" aria-pressed="true">Explorer</button>
    <button id="tab-stats" aria-pressed="false">Statistiques</button>
  </nav>
</header>
<main>
  <div id="browse">
    <div class="crumbs" id="crumbs"></div>
    <div class="resume" id="resume"></div>
    <ul id="listing" role="listbox"></ul>
  </div>
  <div id="stats" hidden></div>
</main>
<footer id="footer"></footer>

<script id="payload" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var D = "d", F = "f", P = "p", N = "n";
  var payload = JSON.parse(document.getElementById("payload").textContent);
  var tree = payload.tree;
  var path = [];

  var listing = document.getElementById("listing");
  var crumbs = document.getElementById("crumbs");

  // -- navigation au clavier ------------------------------------------------
  // rows : les lignes du dossier courant, dans l'ordre d'affichage, pour que
  // les flèches et la recherche incrémentale puissent s'y déplacer.
  var rows = [];
  var selectedIndex = -1;
  var pendingReselect = null;
  var typeahead = { buffer: "", timer: null };

  function nodeAt(segments) {
    var node = tree;
    for (var i = 0; i < segments.length; i++) {
      var children = node[D];
      if (!children || !children[segments[i]]) return null;
      node = children[segments[i]];
    }
    return node;
  }

  function urlFor(segments, leaf) {
    // Le premier segment est l'hôte ; le reste forme le chemin.
    if (!segments.length) return null;
    var base = "https://" + segments[0] + "/";
    var rest = segments.slice(1);
    if (leaf === undefined) return base + (rest.length ? rest.join("/") + "/" : "");
    return base + (rest.length ? rest.join("/") + "/" : "") + leaf;
  }

  function fmt(n) {
    return String(n).replace(/\\B(?=(\\d{3})+(?!\\d))/g, "'");
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderCrumbs() {
    crumbs.textContent = "";
    var racine = el("a", null, "racine");
    racine.onclick = function () { go([]); };
    crumbs.appendChild(racine);
    path.forEach(function (segment, i) {
      crumbs.appendChild(el("span", "sep", "/"));
      var link = el("a", null, segment);
      link.onclick = function () { go(path.slice(0, i + 1)); };
      crumbs.appendChild(link);
    });
  }

  function row(icon, label, opts) {
    var li = el("li", opts.dead ? "dead" : null);
    li.appendChild(el("span", "ico", icon));
    var name = el("span", "name");
    var link = el("a", null, label);
    if (opts.href) { link.href = opts.href; link.target = "_blank"; link.rel = "noopener"; }
    else if (opts.onclick) { link.onclick = opts.onclick; }
    name.appendChild(link);
    li.appendChild(name);
    if (opts.dead) li.appendChild(el("span", "tag", "morte"));
    if (opts.count !== undefined) li.appendChild(el("span", "count", fmt(opts.count) + " URLs"));
    return li;
  }

  // Tout est dérivé des champs déjà présents (d, f, p, n) : aucune donnée
  // supplémentaire n'est ajoutée au JSON embarqué pour cet affichage détaillé.
  function renderResume(node) {
    var host = document.getElementById("resume");
    host.textContent = "";
    if (!node) return;

    var direct = (node[F] || []).length + (node[P] !== undefined ? 1 : 0);
    var sousDossiers = Object.keys(node[D] || {}).length;
    var total = node[N] || 0;

    var rubrique = "non";
    if (node[P] === 0) rubrique = "oui, vivante";
    else if (node[P] === 1) rubrique = "oui, morte";

    [
      ["rubrique (./)", rubrique],
      ["URLs directes", fmt(direct)],
      ["sous-dossiers", fmt(sousDossiers)],
      ["URLs dans les sous-dossiers", fmt(total - direct)],
      ["total", fmt(total)]
    ].forEach(function (pair) {
      var item = el("span", "item");
      item.appendChild(document.createTextNode(pair[0] + " : "));
      item.appendChild(el("b", null, pair[1]));
      host.appendChild(item);
    });
  }

  function renderListing() {
    listing.textContent = "";
    rows = [];
    selectedIndex = -1;
    var node = nodeAt(path);
    renderResume(node);
    if (!node) { listing.appendChild(el("li", "empty", "Dossier introuvable.")); return; }

    function addRow(li, label) {
      li.setAttribute("role", "option");
      listing.appendChild(li);
      rows.push({ li: li, label: label });
    }

    // La page du dossier lui-même, quand elle existe, en tête de liste.
    if (node[P] !== undefined) {
      addRow(row("@", "(cette rubrique)", {
        href: urlFor(path), dead: node[P] === 1
      }), "(cette rubrique)");
    }

    var dirs = node[D] || {};
    Object.keys(dirs).sort().forEach(function (name) {
      addRow(row("/", name + "/", {
        onclick: (function (n) { return function () { go(path.concat([n])); }; })(name),
        count: dirs[name][N]
      }), name);
    });

    (node[F] || []).forEach(function (entry) {
      addRow(row("\\u00b7", entry[0], {
        href: urlFor(path, entry[0]), dead: entry[1] === 1
      }), entry[0]);
    });

    if (!rows.length) {
      listing.appendChild(el("li", "empty", "Dossier vide."));
    } else {
      var start = 0;
      if (pendingReselect !== null) {
        var found = indexOfLabel(pendingReselect);
        if (found >= 0) start = found;
      }
      select(start);
    }
    pendingReselect = null;
  }

  function indexOfLabel(label) {
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].label === label) return i;
    }
    return -1;
  }

  function select(index) {
    if (!rows.length) { selectedIndex = -1; return; }
    index = Math.max(0, Math.min(index, rows.length - 1));
    if (selectedIndex >= 0 && rows[selectedIndex]) {
      rows[selectedIndex].li.classList.remove("selected");
      rows[selectedIndex].li.removeAttribute("aria-selected");
    }
    selectedIndex = index;
    var entry = rows[selectedIndex];
    entry.li.classList.add("selected");
    entry.li.setAttribute("aria-selected", "true");
    entry.li.scrollIntoView({ block: "nearest" });
  }

  // Ouvre la ligne sélectionnée en rejouant le clic de son lien : évite de
  // dupliquer la logique (dossier -> go(), fichier -> nouvel onglet) déjà
  // portée par row().
  function activateSelected() {
    if (selectedIndex < 0) return;
    var link = rows[selectedIndex].li.querySelector("a");
    if (link) link.click();
  }

  function goBack() {
    if (!path.length) return;
    go(path.slice(0, -1), path[path.length - 1]);
  }

  function go(segments, reselect) {
    path = segments;
    pendingReselect = reselect !== undefined ? reselect : null;
    location.hash = segments.length ? "#/" + segments.join("/") : "";
    renderCrumbs();
    renderListing();
  }

  function fromHash() {
    var raw = decodeURIComponent(location.hash.replace(/^#\\/?/, ""));
    return raw ? raw.split("/").filter(Boolean) : [];
  }

  // -- statistiques --------------------------------------------------------

  var LABELS = {
    urls: "URLs indexées",
    vivantes_ou_non_verifiees: "Vivantes ou non vérifiées",
    mortes: "Mortes (404/410)",
    dossiers: "Dossiers",
    anomalies: "Anomalies"
  };

  function renderStats() {
    var host = document.getElementById("stats");
    host.textContent = "";

    host.appendChild(el("h2", null, "Vue d'ensemble"));
    var wrap = el("div", "overflow");
    var table = el("table");
    Object.keys(LABELS).forEach(function (key) {
      if (payload.stats[key] === undefined) return;
      var tr = el("tr");
      tr.appendChild(el("th", null, LABELS[key]));
      tr.appendChild(el("td", "num", fmt(payload.stats[key])));
      table.appendChild(tr);
    });
    wrap.appendChild(table);
    host.appendChild(wrap);

    host.appendChild(el("h2", null, "Par rubrique"));
    var rows = [];
    Object.keys(tree[D] || {}).forEach(function (hostname) {
      var sections = tree[D][hostname][D] || {};
      Object.keys(sections).forEach(function (name) {
        rows.push([hostname + "/" + name, sections[name][N]]);
      });
    });
    rows.sort(function (a, b) { return b[1] - a[1]; });

    var wrap2 = el("div", "overflow");
    var table2 = el("table");
    var head = el("tr");
    head.appendChild(el("th", null, "Rubrique"));
    head.appendChild(el("th", null, "URLs"));
    table2.appendChild(head);
    rows.forEach(function (entry) {
      var tr = el("tr");
      tr.appendChild(el("td", null, entry[0]));
      tr.appendChild(el("td", "num", fmt(entry[1])));
      table2.appendChild(tr);
    });
    wrap2.appendChild(table2);
    host.appendChild(wrap2);
  }

  // -- onglets -------------------------------------------------------------

  var tabBrowse = document.getElementById("tab-browse");
  var tabStats = document.getElementById("tab-stats");

  function show(which) {
    var stats = which === "stats";
    document.getElementById("browse").hidden = stats;
    document.getElementById("stats").hidden = !stats;
    tabBrowse.setAttribute("aria-pressed", String(!stats));
    tabStats.setAttribute("aria-pressed", String(stats));
    if (stats) renderStats();
  }

  tabBrowse.onclick = function () { show("browse"); };
  tabStats.onclick = function () { show("stats"); };
  window.onhashchange = function () { go(fromHash()); };

  // Cherche, à partir de `start` et en bouclant sur la liste, le premier
  // élément dont le nom commence par `needle`. -1 si aucun.
  function chercherPrefixe(needle, start) {
    for (var i = 0; i < rows.length; i++) {
      var idx = (start + i) % rows.length;
      if (rows[idx].label.toLowerCase().indexOf(needle) === 0) return idx;
    }
    return -1;
  }

  // Flèches pour se déplacer, Origine/Fin pour les extrémités, Entrée pour
  // ouvrir, Retour arrière pour remonter, et une recherche incrémentale
  // façon Explorateur Windows : taper des lettres allonge le texte recherché
  // et saute au prochain élément qui commence par ce texte complet. Si rien
  // n'y correspond *et* que toutes les lettres tapées jusqu'ici sont
  // identiques (ex. répéter "1" pour atteindre "110" sans qu'aucun élément
  // ne commence par "11"), on bascule sur un simple défilement des éléments
  // commençant par cette seule lettre — sans quoi répéter une lettre pour
  // "faire défiler" resterait bloqué dès que deux répétitions ne
  // correspondent à rien.
  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable)) return;
    if (document.getElementById("stats").hidden === false) return;  // onglet stats actif

    if (e.key === "ArrowDown") { e.preventDefault(); select(selectedIndex + 1); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); select(selectedIndex - 1); return; }
    if (e.key === "Home") { e.preventDefault(); select(0); return; }
    if (e.key === "End") { e.preventDefault(); select(rows.length - 1); return; }
    if (e.key === "Enter") { e.preventDefault(); activateSelected(); return; }
    if (e.key === "Backspace") { e.preventDefault(); goBack(); return; }

    if (e.key.length === 1 && /[\\p{L}\\p{N}]/u.test(e.key)) {
      var ch = e.key.toLowerCase();
      clearTimeout(typeahead.timer);
      typeahead.buffer += ch;
      typeahead.timer = setTimeout(function () { typeahead.buffer = ""; }, 900);

      var start = selectedIndex >= 0 ? selectedIndex + 1 : 0;
      var trouve = chercherPrefixe(typeahead.buffer, start);

      var repeteLaMemeLettre = typeahead.buffer.split("").every(function (c) { return c === ch; });
      if (trouve < 0 && repeteLaMemeLettre) {
        trouve = chercherPrefixe(ch, start);
      }
      if (trouve >= 0) select(trouve);
    }
  });

  document.getElementById("footer").textContent =
    fmt(payload.stats.urls) + " URLs \\u00b7 g\\u00e9n\\u00e9r\\u00e9 le " + payload.generated_at;

  go(fromHash());
})();
</script>
</body>
</html>
"""
