# RTS-indexer

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
- **`rss`** — les flux RSS d'une vingtaine de rubriques éditoriales. Seule source qui capte les
  articles au fil de leur publication ; leur fenêtre est courte (~24h pour les plus actives), une
  exécution quotidienne est le minimum pour ne rien manquer.
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
| `rss` | Collecte les articles récents depuis les flux RSS des rubriques. |
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

### `rss`

```bash
python -m rts_indexer rss [--dry-run] [--limit N]
```

Interroge les flux RSS des rubriques listées dans `config.RSS_FEEDS` (une vingtaine, curée à la
main — toutes les rubriques n'en exposent pas). Chaque flux ne couvre que ses ~25 dernières
publications : pour la rubrique la plus active (`info/toute-info`), cela représente environ 24h.
C'est la seule source qui voit un article entre le moment où il est publié et celui où il quitte la
première page de sa rubrique — le `crawl` ne verra jamais au-delà, `robots.txt` interdisant la
pagination. À exécuter au moins une fois par jour pour ne rien manquer.

### `crawl`

```bash
python -m rts_indexer crawl [--max-pages N] [--include-articles] [--reset]
```

- `--max-pages` (défaut 500) — plafond exact de pages visitées. `0` = illimité ; le crawl s'arrête
  de lui-même une fois toutes les rubriques connues visitées, la file n'étant pas infinie.
- `--include-articles` — visite aussi les articles eux-mêmes (coûteux), pour en extraire les
  liens connexes. Par défaut ils sont indexés sans être téléchargés.
- `--reset` — oublie la rotation des graines enregistrée, repart du début.

Un checkpoint périodique (toutes les 200 pages) écrit l'index sur disque en cours de route.

**Rotation des graines.** L'index compte des dizaines de milliers de rubriques ; avec un budget
(`--max-pages` non nul), un run planifié régulièrement ne verrait donc jamais que les premières de
la liste, toujours les mêmes. Un curseur persisté (`.cache/crawl_seed_cursor.json`) fait avancer la
tranche de graines à chaque exécution, pour qu'un enchaînement de runs couvre à terme la totalité
des rubriques plutôt que de piétiner. `--max-pages 0` (illimité) ignore la rotation : un run sans
budget couvre déjà tout.

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

`build` est la seule commande qui réécrit **tous** les fichiers, y compris ceux dont le contenu
n'a pas changé (voir ci-dessous). C'est ce qui lui permet d'appliquer un changement de seuil de
sharding ou de projection des chemins, lesquels ne modifient rien en mémoire et resteraient donc
sans effet autrement.

## Écriture sélective

`data/` n'est pas réécrit intégralement à chaque commande : seuls les dossiers dont le contenu a
réellement changé depuis le chargement sont touchés. Ajouter trois URLs ne réécrit plus 138'000
fichiers avec un contenu identique.

Le contenu d'un dossier modifié, lui, reste **recalculé en entier** — aucune logique incrémentale
de découpage de shard. Le déterminisme est donc préservé, et même renforcé : un run sans nouveauté
amont ne touche plus aucun fichier, là où il les réécrivait auparavant à l'identique.

Le point délicat est que `_prune()` supprime tout fichier d'index qu'il ne reconnaît pas comme
légitime. Pour un dossier qu'on ne réécrit pas, on ne *recalcule* donc pas quels fichiers
devraient exister — un tel calcul, en divergeant de la réalité sur un seul cas, effacerait les
fichiers concernés. On réutilise ceux qui ont été **observés** sur disque au chargement. Comme
aucune URL n'est jamais retirée de l'index (`verify` ne fait que poser ou retirer le sigil `!`),
un dossier inchangé a nécessairement les bons fichiers sur disque.

## Automatisation (GitHub Actions)

Trois cadences, parce que les sources n'ont pas du tout le même profil de coût.
Toutes committent et poussent elles-mêmes ce qu'elles trouvent, et partagent la même
mécanique (`_collecte.yml`, workflow réutilisable) : checkout, curseurs, commit, push.

| Workflow | Cadence | Commandes | Pourquoi ce rythme |
| --- | --- | --- | --- |
| `rss.yml` | 2×/jour | `rss` | La fenêtre des flux est de ~24h ; deux passages laissent une marge aux retards du planificateur. |
| `hebdomadaire.yml` | lundi | `sitemap`, `crawl`, `verify` | Entretien de la structure et contrôle de vivacité, par tranches budgétées. |
| `archives.yml` | mensuel + manuel | `wayback` | Requêtes lentes, parcours par tranches sur des semaines. |

Un groupe de concurrence partagé (`index-ecriture`) les sérialise : ils écrivent tous dans
`data/` et poussent sur la même branche.

**Avant la première exécution**, deux points à régler côté dépôt :

1. *Settings → Actions → General → Workflow permissions* doit être sur **Read and write
   permissions**, sans quoi le `git push` échoue en 403.
2. Lancer chaque workflow **à la main** (`workflow_dispatch`) une première fois. Les budgets
   (`--max-pages`, `--limit`) sont volontairement prudents : chaque commande paie un cycle
   complet de relecture/réécriture de l'index, dont le coût sur un runner Linux n'a pas encore
   été mesuré. Les relever une fois les vrais temps connus.

Deux comportements de GitHub Actions à garder en tête : un `cron` est « au mieux » et peut être
retardé de plusieurs dizaines de minutes aux heures chargées ; et les workflows planifiés sont
désactivés automatiquement après 60 jours sans activité sur le dépôt.

Les curseurs de reprise (rotation des graines du crawl, progression Wayback) vivent dans
`.cache/`, qui n'est pas versionné : ils sont persistés d'une exécution à l'autre par
`actions/cache`, et sauvegardés même en cas d'échec pour ne pas perdre des heures de collecte.

## Tests

```bash
pytest
```
