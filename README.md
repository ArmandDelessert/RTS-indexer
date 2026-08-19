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
| `dedupe` | Supprime les URLs journalisées comme doublons par `verify`. |
| `purge` | Supprime de l'index les URLs actuellement marquées mortes. |
| `import` | Ajoute à l'index des URLs listées dans un fichier texte. |
| `anomalies` | Inventorie et nettoie le journal d'anomalies. |
| `build` | Relit et réécrit `data/` (tri, sharding, purge) sans rien collecter. |
| `site` | Génère la page web de consultation (`site/index.html`). |
| `stats` | Affiche les compteurs de l'index. |
| `list` | Reconstruit et affiche les URLs complètes depuis `data/`. |
| `run` | Enchaîne plusieurs commandes sur un seul chargement/écriture. |

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
python -m rts_indexer verify [--limit N] [--recheck-days N] [--path PREFIXE] [--dead-only]
```

- `--limit` — nombre maximal d'URLs à contrôler, les jamais-vues d'abord (0 = toutes).
- `--recheck-days` (défaut 30) — âge au-delà duquel une URL déjà contrôlée l'est de nouveau.
- `--path` — ne contrôler que les URLs sous ce préfixe (`www.rts.ch/meteo/` ou l'URL complète).
  Pratique pour valider un changement sans lancer un run complet.
- `--dead-only` — ne recontrôler que les URLs déjà marquées mortes (sigil `!`), sans égard pour
  `--recheck-days` : un audit ponctuel, pas le flux incrémental habituel. À combiner avec `purge`
  pour matérialiser les verdicts confirmés en suppressions réelles.

Il n'y a pas de curseur de reprise : `.cache/verify.json` mémorise la date et le code de chaque
contrôle, et chaque exécution attaque en priorité les URLs jamais vues, puis celles dont le
contrôle dépasse `--recheck-days`. Plus robuste qu'un curseur positionnel, qui se désynchroniserait
dès que l'index change de taille.

Verdict volontairement prudent : seuls 404 et 410 marquent une URL morte. Un 403, un 429 ou une
erreur serveur sont non concluants (ni cache, ni changement de sigil) — rts.ch renvoie par
exemple un 403 sur des pages bien vivantes.

**Second avis.** Un 404 isolé ne condamne pas : l'URL est remise dans une file à échéance et
recontrôlée une minute plus tard, le sigil n'étant posé que si le second avis confirme. Les
réponses non concluantes suivent le même chemin — elles sont transitoires par nature, mieux vaut
réessayer dans la minute qu'au prochain run. Le 410 en est exclu : c'est une suppression explicite
du serveur (mesuré : 70 % des URLs mortes), la réinterroger serait du gaspillage. L'attente
n'immobilise aucun worker, le parcours continue pendant ce temps.

**Doublons.** Le client suit les redirections ; comparer l'URL finale à l'URL demandée démasque
les variantes de slug qui pointent vers le même article — elles répondent 200 et paraissent donc
saines. Elles sont journalisées dans `_anomalies.tsv` (type `doublon`) sans être supprimées à ce
stade : le serveur seul sait quelle forme est canonique, et sa règle est contre-intuitive
(`...traversent-elles-l-atlantique.html` redirige vers `...traversentelles-latlantique.html`).
Coût réseau nul, l'information transitait déjà. La suppression effective se fait ensuite via
`dedupe`.

### `dedupe`

```bash
python -m rts_indexer dedupe
```

Supprime les URLs journalisées comme doublons (`verify` en pose le constat, `dedupe` en tire la
conséquence). Si la cible de la redirection n'est pas encore indexée, elle l'est d'abord — `verify`
a déjà obtenu un vrai 200 dessus au moment de constater la redirection, ce n'est pas une
supposition. C'est d'ailleurs le cas majoritaire en pratique : lors du premier passage réel sur
l'index, 5'807 doublons sur 5'856 cibles manquantes pointaient vers une vraie page rts.ch jamais
collectée, contre 48 seulement vers un hôte hors périmètre (`img.rts.ch`).

Cette confiance est filtrée, pas aveugle : la cible passe par `urlnorm.normalize()`, le même
contrôle de périmètre qu'utilisent toutes les autres sources, avant d'être indexée. Sans ce filtre,
un identifiant qui redirige vers une image sur `img.rts.ch` entrerait dans l'index sans contrôle —
`Store.add()` seul ne vérifie ni l'hôte ni l'extension. Une cible qui échoue à ce filtre n'est pas
un vrai doublon — rien dans l'index n'en fait double emploi, elle redirige simplement hors
périmètre — donc `dedupe` ne supprime rien : l'anomalie est **requalifiée** `hors_perimetre` plutôt
que laissée sous l'étiquette `doublon`, trompeuse pour ce cas.

### `purge`

```bash
python -m rts_indexer purge
```

Supprime de l'index les URLs actuellement marquées mortes (sigil `!`). **Ce n'est pas le
comportement par défaut du projet** : une URL morte reste normalement indexée, pour garder la trace
qu'un contenu a existé même après sa disparition — l'intérêt d'un index qui couvre aussi
l'historique, pas seulement ce qui répond aujourd'hui. `purge` existe pour qui préfère
explicitement un index de contenu vivant ; l'historique reste de toute façon récupérable via
l'historique Git.

Ne recontrôle rien — se fie au sigil tel qu'il est en mémoire. L'usage prévu est en deux temps :

```bash
python -m rts_indexer verify --dead-only   # reconfirme (ou ressuscite) chaque URL morte connue
python -m rts_indexer purge                # supprime celles qui le sont encore
```

### `import`

```bash
python -m rts_indexer import fichier.txt [--check] [--dry-run] [--limit N]
```

Ajoute des URLs relevées à la main — typiquement repérées via un moteur de recherche, dont aucune
source ne ramène le lien faute de page vivante qui y pointe encore. Une URL par ligne dans
`fichier.txt` ; `#` en début de ligne commente, les lignes vides sont ignorées.

`--check` contrôle chaque URL avant l'ajout (une requête chacune, au même débit poli que
`verify`) : les mortes sont écartées, et une redirection fait indexer la **cible** plutôt que
l'URL demandée — sinon on introduirait soi-même un doublon que `dedupe` devrait retirer ensuite.
Sans `--check`, une liste tapée à la main peut faire entrer des URLs mortes ou obsolètes ; avec,
le surcoût est d'une requête par URL, raisonnable pour le volume typique d'un import manuel
(quelques dizaines d'URLs) — pas pour les sources automatiques, qui publient des URLs déjà
canoniques et vivantes par construction.

### `anomalies`

```bash
python -m rts_indexer anomalies [--check] [--drop-dead] [--drop-out-of-scope]
```

Sans option, inventorie `_anomalies.tsv` par type. `--check` contrôle les URLs concernées ;
`--drop-dead` (qui implique `--check`) retire du journal celles confirmées mortes, et de l'index
si elles y étaient (cas `hors_perimetre` — `trop_long` n'y a jamais pu entrer).
`--drop-out-of-scope` retire les `hors_perimetre` sans recontrôle réseau : contrairement à la
vivacité, « redirige hors périmètre » est un fait structurel (mauvais hôte, mauvais format) qui
ne change pas — le constat déjà posé par `verify`/`dedupe` fait foi.

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

`build` est la seule commande qui réécrit **tous** les fichiers et balaye **tout** l'arbre (voir
ci-dessous). C'est ce qui lui permet d'appliquer un changement de seuil de sharding ou de
projection des chemins — lesquels ne modifient rien en mémoire et resteraient donc sans effet
autrement — et de rattraper une dérive externe. Il est lancé chaque semaine par le workflow
`hebdomadaire.yml` pour cette raison.

`stats` affiche :

| Compteur | Signification |
| --- | --- |
| `urls` | Total indexé, vivantes et mortes confondues. |
| `vivantes_ou_non_verifiees` | `urls` moins `mortes`. Le nom est double à dessein : une URL jamais passée par `verify` compte comme vivante ici, faute de verdict contraire. |
| `mortes` | URLs pour lesquelles `verify` a obtenu un 404 ou 410 confirmé (sigil `!`). Reste petit tant que `verify` n'a tourné que sur un échantillon — ce n'est pas « peu d'URLs mortes », c'est « peu d'URLs *contrôlées*. » |
| `dossiers` | Segments de chemin distincts sous `data/` (une entrée par `_index.txt`, hors shards). |
| `anomalies` | Lignes dans `_anomalies.tsv`, de quatre types : `trop_long` (chemin projeté au-delà de `MAX_REL_PATH_LEN`), `collision` (deux URLs ne différant que par la casse visent le même chemin disque, NTFS étant insensible à la casse), `doublon` (l'URL redirige vers une autre du périmètre, constaté par `verify` — actionnable par `dedupe`), et `hors_perimetre` (l'URL redirige hors périmètre, ex. vers `img.rts.ch` — ce n'est pas un vrai doublon, rien dans l'index n'en fait double emploi, et `dedupe` ne le résoudra jamais). |

### `run`

```bash
python -m rts_indexer run [--file commandes.txt]     # sinon, lit l'entrée standard
```

Enchaîne plusieurs commandes sur un seul chargement et une seule écriture de l'index, au lieu d'un
cycle complet — plusieurs minutes — par commande. Une commande complète par ligne, avec ses
propres options ; `#` commente, les lignes vides sont ignorées :

```
sitemap
crawl --max-pages 1500
verify --limit 2000
build
```

La chaîne s'arrête à la première commande qui échoue (code de retour non nul), et écrit alors ce
qui a été accumulé jusque-là. Chaque commande garde sa propre résilience réseau : une interruption
ou une erreur au milieu d'une collecte (`crawl`, `wayback`, `verify`...) écrit immédiatement,
sans attendre une fin de chaîne qui n'arriverait pas. `build`, de même, écrit toujours sur-le-champ
— différer son `force=True` à la fin de la chaîne le viderait de son sens.

C'est le format qu'utilise déjà `_collecte.yml` (une commande par ligne) : le workflow pourrait
n'invoquer qu'un seul `run` plutôt que de boucler côté shell sur chaque ligne.

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
fichiers concernés. On réutilise ceux qui ont été **observés** sur disque au chargement. L'invariant
qui rend ça sûr n'est pas « aucune URL n'est jamais retirée » (`remove()` en retire, pour éliminer
des doublons — voir plus bas) mais plus précis : *un dossier non marqué sale a les bons fichiers sur
disque*. `add()` et `remove()` marquent systématiquement sale tout dossier dont le contenu change
réellement ; un dossier non touché n'a, par construction, aucune raison d'avoir divergé de ce qui a
été observé au chargement.

### Purge ciblée

Même raisonnement pour la suppression. La purge n'examine plus tout l'arbre à chaque commande —
ses deux parcours de 138'000 dossiers représentaient, une fois l'écriture devenue sélective,
l'essentiel du temps restant. Elle ne visite que les dossiers susceptibles de porter un orphelin :
ceux dont un fichier était illisible au chargement (leur contenu est perdu, le fichier doit
partir) et ceux devenus entièrement vides après un `remove()`. Les dossiers réécrits se purgent
déjà eux-mêmes au passage, et un dossier ni sale ni vidé n'a rien à purger.

La contrepartie est réelle et assumée : une **dérive externe** — fichier déposé à la main dans
`data/`, reste d'une fusion Git, débris d'une écriture interrompue — n'est plus corrigée à chaque
commande, mais au prochain `build`. D'où sa présence dans le workflow hebdomadaire.

Un `Store` sur lequel `load()` n'a jamais été appelé retombe automatiquement sur le balayage
complet : sans la connaissance du disque accumulée au chargement, la purge ciblée n'aurait aucune
base pour distinguer un fichier légitime d'un orphelin.

### Suppressions par lots

Incident réel : sous Windows, un antivirus dont la protection en temps réel est active verrouille
brièvement chaque fichier qu'il scanne — y compris juste après une suppression, pendant qu'il
examine l'événement. `fsutil.retry()` (5 tentatives, quelques centaines de millisecondes entre
chacune) suffit pour un fichier isolé, mais épuiser ce cycle complet *par élément* devient
ruineux dès qu'il faut purger un grand nombre de petits dossiers d'un coup — un `dedupe` qui en
libère plus d'un millier a ainsi tourné près de 10 heures pour un taux de réussite de 0 % sur la
suppression des dossiers, chaque tentative individuelle butant sur le même type de verrou avant de
passer, silencieusement, au suivant.

`fsutil.retry_many()` regroupe les nouvelles tentatives par lots plutôt que par élément : tout ce
qui échoue à un tour est retenté ensemble au tour suivant, après une seule pause. Le coût total de
l'attente devient celui de quelques tours (secondes), pas celui de mille cycles de tentatives
individuelles (potentiellement des heures) — et surtout, un échec qui persiste après tous les tours
est **journalisé explicitement**, plutôt qu'avalé par un `except OSError: break` qui rendait un
taux d'échec total indiscernable d'une progression normale.

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
