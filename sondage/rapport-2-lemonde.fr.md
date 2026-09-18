# Sonde n°2 — www.lemonde.fr

Requêtes en direct du 18 septembre 2026, User-Agent
`url-indexer-probe/0.1 (+https://github.com/ArmandDelessert/RTS-indexer)`,
robots.txt relu et appliqué avant chaque requête (30 Disallow, dont trois
motifs à query : `/*?contributions`, `/*?s=43260*`,
`/recherche/?*search_keywords=*` — jamais sollicités), 1 requête/s au plus,
jamais en parallèle. **75 requêtes** au total : 62 × 200, 8 × 301, 5 × 404
(les 301 et 404 sont attendus ou provoqués, voir plus bas). Aucune erreur réseau.

## Question 1 — La query string est-elle porteuse ?

### Liens à query réellement utilisés

Extraction des liens internes de `/`, `/international/` et `/economie/` :
**41 URLs distinctes** avec query. Paramètres rencontrés (occurrences) :

| paramètre | occ. | nature |
|---|---|---|
| `lmd_campaign` / `lmd_medium` / `lmd_creation` / `lmd_variant` | 52 / 52 / 51 / 42 | tracking interne (services : Mémorable, guides d'achat, newsletters) |
| `preferred_lang` | 12 | bascule FR/EN du menu |
| `question`, `thematic` | 3 / 3 | navigation de `/faq/` |
| `a9epi` | 2 | jeton d'inscription Mémorable |
| `srsltid`, `utm_*` | 1 / 1 | tracking externe |

Les pages de rubrique ne lient **aucune** URL éditoriale à query : la
pagination officielle est dans le chemin (`/international/2`, sans slash
final, cf. `a.river__pagination`).

### Comparaison avec / sans query (14 paires)

| URL testée | statuts | texte identique | canonical |
|---|---|---|---|
| `/?preferred_lang=fr` vs `/` | 200/200 | oui (ratio 1.00) | `https://www.lemonde.fr` |
| `/faq/?question=…` vs `/faq/` | 200/200 | oui | — |
| `/faq/?thematic=contacter` vs `/faq/` | 200/200 | oui | — |
| `/climat/meteo/?lmd_medium=…` | 200/200 | oui | `/climat/meteo/` |
| `/memorable/quiz-…?lmd_campaign=…` | 200/200 | oui | sans query |
| `/guides-d-achat/?lmd_campaign=…` | 200/200 | oui | `/guides-d-achat/` |
| `/memorable/boutique/gift/?lmd_…` | 200/200 | oui | `…/gift/?language=fr` (boutique, hors périmètre éditorial) |
| 3 articles `…html?lmd_campaign=…` / `?srsltid=…` / `?utm_source=…` | 301/301 | — | la redirection **conserve** la query mais pointe vers la même cible avec ou sans |
| `/international/?page=2` vs `/international/` | 200/200 | **non** (0.07) | `/international/` |
| `/international/?page=2` vs `/international/2` | 200/200 | oui (1.00) | `/international/` |
| `/international/?page=3` vs `/international/3` | 200/**404** | — | la forme chemin s'arrête à 2 pages, `?page=N` est un alias non lié qui continue |
| `/economie/?page=2` vs `/economie/` | 200/200 | non (0.04) | `/economie/` |

Le seul paramètre qui change le contenu est `page=`, et il n'est qu'un alias
serveur d'une forme **chemin** officielle (`/section/2`) — la canonical de la
page 2 pointe d'ailleurs vers la page 1. Tout le reste est du tracking ou de
la navigation JavaScript sans effet côté serveur.

**Conclusion : la query est toujours jetable sur lemonde.fr, exactement comme
sur rts.ch.** Aucun paramètre à conserver dans le profil.

### Effet de bord à connaître : les 301 de re-datation

Les trois articles testés avec query ont répondu 301 — pas à cause de la
query, mais parce que Le Monde **re-date** un article mis à jour :
`/guides-d-achat/article/2025/03/10/les-meilleurs-couteaux-de-chef_6578271_5306571.html`
→ `/guides-d-achat/article/2026/06/29/les-meilleurs-couteaux-de-chef_6578271_5306572.html`.
Le premier identifiant (`6578271`) est stable ; la date, le slug et le second
identifiant (rubrique) peuvent changer. Une URL sitemap ancienne peut donc
rediriger vers un autre dossier `AAAA/MM/JJ` : l'index accumulera des doublons
d'articles sous plusieurs dates si le moteur ne suit pas les 301 et ne déduplique
pas sur le premier identifiant.

## Question 2 — Rubrique vs article (rappel, non demandé pour ce site)

La règle actuelle « point dans le segment terminal ⇒ ligne » tient sur
**99.35 %** des URLs de l'échantillon sitemap (43 236 / 43 519 URLs
`www.lemonde.fr`). Les exceptions, toutes mesurées :

- **Blogs** (`/<nom-du-blog>/AAAA/MM/JJ/<slug>/`, slash final, sans `.html`,
  sans segment de type) : 283 URLs sur 43 519 (0.65 %), de 2005 à 2024
  (`langue-sauce-piquante`, `liberons-les-crayons`, `une-annee-au-lycee`,
  `cuisines-de-l-assemblee`, `realites-biomedicales`, `lheure-du-monde`…).
  Elles répondent 200 (2/2 testées) et seraient aujourd'hui traitées comme
  des **dossiers** : ~11 600 dossiers parasites par extrapolation sur
  1944-2026.
- **Pagination de rubrique** `/international/2` : pas de point, pas de slash
  → `urlnorm` produirait `/international/2/` qui répond **404** (testé ; seule
  la forme sans slash existe). Idem `/international` (sans slash) → 301 vers
  `/international/`.
- Les niveaux intermédiaires du gabarit n'existent pas comme pages :
  `/international/article/` → 404, `/international/article/2026/` → 404,
  `/international/article/2026/09/18/` → 301 vers `/international/`,
  `/langue-sauce-piquante/2024/03/` → 404. En revanche `/archives/` et
  `/langue-sauce-piquante/` (niveau 1) sont de vraies pages de listing (200).

Règle déclarative complémentaire suggérée :

> `/<blog>/AAAA/MM/JJ/<slug>/` (quatre segments après le premier, dont trois
> numériques) ⇒ **ligne** dans `<blog>/AAAA/MM/JJ/_index`, malgré l'absence
> d'extension. Segment terminal purement numérique à profondeur 2
> (`/section/2`) ⇒ **ligne** de pagination (ou exclusion), et surtout **sans
> slash final** à la reconstruction.

## Question 3 — Ampleur réelle

### Structure des sitemaps

`sitemap_index.xml` (427 Ko) déclare **3 296 sitemaps enfants**, en six
familles plus 11 sitemaps spéciaux :

| famille | fichiers | période | granularité |
|---|---|---|---|
| `sitemap/year/articles/AAAA-MM-01.xml` | **982** | 1944-12 → 2026-09 | mensuelle, couverture continue (982 mois) |
| `sitemap/articles/AAAA-MM-JJ.xml` | 869 | 1945-04 → 2026-09 | hebdomadaire (tous des lundis), lacunaire avant 2021 — **sous-ensemble** du mensuel (784 + 801 URLs pour deux semaines de mars 2024 vs 3 450 pour le mois) |
| `sitemap/year/videos/…` | 234 | 2007-03 → 2026-09 | mensuelle (61 URLs pour 2024-03) |
| `sitemap/videos/…` | 571 | 2007-09 → 2026-09 | hebdomadaire |
| `sitemap/year/images/…` | 235 | 2005-03 → 2026-09 | mensuelle (1 URL pour 2024-03 : résiduel) |
| `sitemap/images/…` | 394 | 2005-03 → 2026-09 | hebdomadaire |
| `sitemap/elections/…`, `sitemap/sport/…` | 11 | 2017 → 2026 | par scrutin/compétition ; `legislatives-2024` seul = **35 188 URLs** `/resultats-legislatives-2024/<region>/<departement>/…/` (profondeur 1-3, slash final, sans extension) |

`/en/sitemap_index.xml` : 436 enfants, 2022-03 → 2026-09, même gabarit
préfixé `/en/` (1 005 URLs pour 2024-03, profondeur 7).

### Volume (11 mois échantillonnés, 1950 → 2026)

| mois | URLs | dossiers `section/type/AAAA/MM/JJ` | sections distinctes |
|---|---|---|---|
| 1950-06 | 2 045 | 26 | 1 (`archives`) |
| 1970-06 | 3 476 | 26 | 1 |
| 1985-06 | 2 708 | 25 | 1 |
| 1995-06 | 3 298 | 28 | 1 |
| 2000-06 | 5 988 | 38 | 6 |
| 2005-06 | 4 888 | 600 | 31 |
| 2010-06 | 5 918 | 824 | 43 |
| 2015-06 | 4 642 | 1 631 | 156 |
| 2020-06 | 2 990 | 696 | 50 |
| 2024-03 | 3 415 | 808 | 57 |
| 2026-08 | 2 448 | 684 | 54 |

Interpolation linéaire entre ces points sur les 982 mois :
**≈ 3,4 millions d'URLs** sur `www.lemonde.fr` (édition FR), auxquelles
s'ajoutent ~15 000 vidéos, ~40 000 URLs `/en/` et 100 à 200 000 pages de
résultats électoraux. Ordre de grandeur : **3,5 M d'URLs**, soit 7 à 8 × les
~459 000 lignes actuellement indexées pour rts.ch (le « 24 000 » du
commentaire de `urlnorm.py` est périmé).

### Conformité au gabarit

Sur 43 519 URLs `www.lemonde.fr` de l'échantillon :
`/section/type/AAAA/MM/JJ/slug_id1_id2.html` = **43 236 (99.35 %)**.
Segment « type » : `article` 42 209, `video` 577, `portfolio` 316,
`live` 102, `visuel` 21, `appel-temoignages` 11. Les lives, vidéos,
portfolios et contenus payants (paywall côté client, HTML de 300 Ko servi
quand même) suivent tous le gabarit. 221 sections distinctes observées
(`archives` = 17 563 URLs, tout ce qui précède ~2000). 1 URL à slug vide
(`_6002830_1616946.html`). Les 283 autres exceptions sont les blogs décrits
plus haut. Hors hôte : `podcasts.lemonde.fr/<emission>/<horodatage>-<slug>`
(présent dans les sitemaps, à écarter par le filtre d'hôte).

### Dossiers produits par le mapping actuel

Le niveau `section/type/AAAA/MM/JJ` domine : **≈ 279 000 dossiers**
extrapolés (33 000 pour 1944-2004 avec une seule section `archives`, soit un
dossier par jour ; 246 000 pour 2005-2026 avec 30 à 160 sections actives par
mois), plus ~14 000 dossiers aux niveaux `section/type/AAAA/MM` et au-dessus,
plus ~11 600 dossiers parasites de blogs. Soit **~300 000 dossiers contre
5 148 pour rts.ch (aujourd'hui 7 839 répertoires / 5 558 fichiers d'index) :
× 40 à × 60**, avec un ratio articles/dossier bien plus faible (3.6 à 4.3
articles par dossier journalier depuis 2020, 8 en 2005, contre ~100 en
régime `archives`). La profondeur 6 est uniforme (7 pour `/en/`), les segments
font au plus 226 caractères, aucun chemin ne dépasse MAX_PATH.

### Anti-bot

- Rafale dédiée : **20 pages d'article en 19 s** (1 req/s), dont 5
  consécutives sur `/international/article/…` puis 2 lives, 4 archives
  1970-2015, 2 blogs, 2 vidéos : **20/20 en 200**, corps complets
  (288 à 447 Ko), pas de Datadome/captcha/challenge dans le HTML, pas d'en-tête
  de limitation.
- Rafale non intentionnelle : 17 sitemaps consécutifs de 0.6 à 4.8 Mo à
  1 req/s : 17/17 en 200.
- Total session : 75 requêtes, zéro 403/429.

Rien ne se déclenche à 1 req/s soutenu ; le paywall reste purement côté
client (les 300 Ko d'HTML sont servis). Au-delà de 1 req/s : non testé.

## Brouillon de règles pour le profil

- **Query** : tout supprimer (comme rts.ch). Pas d'exception.
- **Hôtes** : `www.lemonde.fr` seulement ; `podcasts.lemonde.fr` apparaît
  dans les sitemaps et est à ignorer.
- **Sitemaps** : ne lire que `sitemap/year/articles/*.xml` (982 fichiers,
  couverture complète) + `year/videos` ; le hebdomadaire est redondant. Les
  sitemaps `elections`/`sport` sont un périmètre à part (35 000 pages de
  résultats pour un seul scrutin).
- **Classification** : extension `.html` ⇒ ligne (99.35 %) ; ajouter
  « `/<seg>/AAAA/MM/JJ/<slug>/` ⇒ ligne » pour les blogs ; « `/section/<N>` »
  ⇒ pagination, à exclure ou à reconstruire sans slash final.
- **Redirections** : suivre les 301 et dédupliquer sur `id1` (premier
  identifiant du slug), sinon un même article existe sous plusieurs dates.
- **Échelle** : prévoir ~3.5 M de lignes et ~300 000 dossiers ; le mapping
  disque actuel tient (profondeur 6, chemins courts) mais l'unité de commit
  « un fichier `_index` par jour et par section » n'a plus rien à voir avec
  rts.ch.
