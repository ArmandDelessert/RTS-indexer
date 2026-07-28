# RTS-URL-indexer

Indexation automatisée des URLs du site [rts.ch](https://www.rts.ch/) : scraping, structuration et
archivage de la totalité des URLs découvrables, stockées **en texte dans ce dépôt Git**, sans
serveur ni base de données externe.

## Architecture en bref

`data/` reflète l'arborescence des URLs : un dossier par segment de chemin, un fichier
`_index.txt` par dossier listant les slugs (une ligne par URL), triés et dédupliqués. Une ligne
`./` signale que le dossier est lui-même une page ; un préfixe `!` signale une URL morte
(404/410, posé par `verify`). Aucune majuscule n'est perdue : elle est percent-encodée dans le nom
de dossier pour rester reconstructible (certaines rubriques de rts.ch sont sensibles à la casse).

Quatre sources alimentent l'index :

- **`sitemap`** — les sitemaps XML déclarés dans `robots.txt`. Rapide, ne couvre que les pages de
  rubrique.
- **`crawl`** — parcours des rubriques connues pour découvrir les articles qu'elles référencent.
  Respecte `robots.txt` (y compris ses wildcards `*`/`$`) ; la pagination des rubriques y étant
  interdite, le crawl ne voit que leur première page.
- **`wayback`** — l'archive historique d'Internet Archive, via son API CDX. C'est la seule source
  qui remonte au-delà de ce que le crawl peut voir aujourd'hui.
- **`commoncrawl`** — l'archive de la fondation Common Crawl, même mécanique CDX. Rendement
  mesuré comme nul sur `www.rts.ch` à ce jour, mais utile si l'index s'étend un jour aux
  sous-domaines (`avecvous.rts.ch`, etc.), bien mieux couverts.

`verify` contrôle ensuite quelles URLs répondent encore, et `site` génère une page web statique
pour parcourir le résultat.

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Sous Windows, si le dépôt est cloné dans un chemin déjà profond, activer les chemins longs pour
Git (les chemins générés restent courts en pratique, mais certains outils l'exigent) :

```bash
git config core.longpaths true
```

## Commandes

Toutes les commandes acceptent `-v`/`--verbose` (journal détaillé) et `--data-dir` (racine de
l'index, défaut `data/`).

```bash
python -m rts_indexer <commande> [options]
```

| Commande | Rôle |
| --- | --- |
| `sitemap` | Collecte les sitemaps XML déclarés dans `robots.txt`. |
| `crawl` | Parcourt les rubriques connues pour découvrir des articles. |
| `wayback` | Collecte l'archive historique (Internet Archive). |
| `commoncrawl` | Collecte l'archive Common Crawl. |
| `verify` | Contrôle quelles URLs répondent encore, pose/retire le sigil `!`. |
| `build` | Relit et réécrit `data/` (tri, sharding, purge) sans rien collecter. |
| `site` | Génère la page web de consultation (`site/index.html`). |
| `stats` | Affiche les compteurs de l'index. |
| `list` | Reconstruit et affiche les URLs complètes depuis `data/`. |

### `sitemap`

```bash
python -m rts_indexer sitemap [--dry-run] [--limit N]
```

`--dry-run` affiche les URLs collectées sans rien écrire sur disque.

### `crawl`

```bash
python -m rts_indexer crawl [--max-pages N] [--include-articles]
```

- `--max-pages` (défaut 500) — plafond exact de pages visitées. `0` = illimité ; le crawl s'arrête
  de lui-même une fois toutes les rubriques connues visitées, la file n'étant pas infinie.
- `--include-articles` — visite aussi les articles eux-mêmes (coûteux), pour en extraire les
  liens connexes. Par défaut ils sont indexés sans être téléchargés.

Un checkpoint périodique (toutes les 200 pages) écrit l'index sur disque en cours de route.

### `wayback`

```bash
python -m rts_indexer wayback [--max-pages N]
```

- `--max-pages` (défaut 50) — budget de pages CDX pour cette exécution. `0` = illimité.

Un parcours complet dépasse 1500 pages, à plusieurs secondes chacune (temps de calcul côté
serveur, pas un délai qu'on impose) : compter plusieurs heures. **`--max-pages` permet justement
de le fractionner** : la progression (curseur) est sauvegardée après chaque page dans
`.cache/wayback_cursor.json`, donc relancer la commande reprend exactement où elle s'était
arrêtée, sans repasser sur ce qui est déjà fait. Rien n'empêche d'enchaîner plusieurs exécutions à
`--max-pages 100` par exemple, ou de le laisser tourner sans limite si le temps ne presse pas.

### `commoncrawl`

```bash
python -m rts_indexer commoncrawl [--max-pages N] [--pages-per-index N] [--max-indexes N]
```

- `--max-pages` (défaut 50) — budget total de pages CDX. `0` = illimité.
- `--pages-per-index` (défaut 5) — budget par crawl Common Crawl. Deux crawls voisins se
  recouvrant beaucoup, mieux vaut en balayer plusieurs que creuser le même.
- `--max-indexes` (défaut 12) — nombre de crawls considérés, du plus récent. `0` = tous (125 à ce
  jour).

### `verify`

```bash
python -m rts_indexer verify [--limit N] [--recheck-days N]
```

- `--limit` — nombre maximal d'URLs à contrôler, les jamais-vues d'abord (0 = toutes).
- `--recheck-days` (défaut 30) — âge au-delà duquel une URL déjà contrôlée l'est de nouveau.

Verdict volontairement prudent : seuls 404 et 410 marquent une URL morte. Un 403, un 429 ou une
erreur serveur sont non concluants (ni cache, ni changement de sigil) — rts.ch renvoie par
exemple un 403 sur des pages bien vivantes.

### `site`

```bash
python -m rts_indexer site [--output DOSSIER]
```

Génère une page HTML autonome (`site/index.html` par défaut, non versionné) : navigation par
rubrique et statistiques globales. S'ouvre directement dans un navigateur, sans serveur.

### `build`, `stats`, `list`

```bash
python -m rts_indexer build          # renormalise data/ sans rien collecter
python -m rts_indexer stats          # compteurs (urls, mortes, dossiers, anomalies...)
python -m rts_indexer list [--limit N]
```

## Tests

```bash
pytest
```
