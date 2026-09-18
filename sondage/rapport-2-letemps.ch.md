# Sonde n°2 — www.letemps.ch

Requêtes en direct du 18 septembre 2026, User-Agent
`url-indexer-probe/0.1 (+https://github.com/ArmandDelessert/RTS-indexer)`,
robots.txt relu et appliqué (7 Disallow : `/user/*`, `/index.php/*`,
`/multi-theme/*`, `/en/*`, `/de/*`, `/aw/*`, `/ph/*` ; pas de Crawl-delay,
pas de Sitemap déclaré), 1 requête/s au plus, jamais en parallèle.
**74 requêtes** : 69 × 200, 1 × 308, 4 × 404 (dont `/sitemap_index.xml`,
sondé à l'aveugle). Zéro 429/403 malgré 45 requêtes
consécutives à 1 req/s (le site est derrière Cloudflare, cf. liens
`/cdn-cgi/`).

## Question 1 — La query string est-elle porteuse ?

### Liens à query réellement utilisés

Liens internes de `/`, `/suisse` et `/economie` : **10 URLs distinctes** avec
query, trois paramètres seulement :

| paramètre | occ. | exemple | nature |
|---|---|---|---|
| `page` | 8 | `/suisse?page=2`, `?page=3`, `?page=100` | **pagination des rubriques** (liens « suivant » / « dernière ») |
| `id` | 3 | `/cdn-cgi/content?id=…` | protection e-mail Cloudflare, pas une page |
| `from_newsletter` | 3 | `/compte/sign_up?from_newsletter=true` | tracking |

Aucun lien d'article n'a de query. Les sitemaps (7 903 URLs lues) n'en ont
aucune non plus.

### Comparaison avec / sans query (9 paires)

| URL testée | statuts | ratio texte | Jaccard liens | canonical de la page avec query |
|---|---|---|---|---|
| `/suisse?page=2` vs `/suisse` | 200/200 | **0.14** | 0.32 | `/suisse?page=2` |
| `/suisse?page=3` | 200/200 | **0.14** | 0.31 | `/suisse?page=3` |
| `/economie?page=2` | 200/200 | **0.16** | 0.28 | `/economie?page=2` |
| `/economie?page=100` | 200/200 | **0.13** | 0.25 | `/economie?page=100` (page pleine : 41 liens) |
| `/suisse?page=1` | 200/200 | 1.00 | 1.00 | `/suisse` |
| `/?page=2` vs `/` | 200/200 | 1.00 | 1.00 | `/` (ignoré sur l'accueil) |
| `/suisse?utm_source=test&utm_medium=x` | 200/200 | 1.00 | 0.88 | `/suisse` |
| `/suisse?page=2&utm_source=x` | 200/200 | 0.14 | 0.28 | `/suisse?page=2` |
| `/compte/sign_up?from_newsletter=true` | 200/200 | 1.00 | 1.00 | `/compte/sign_up` |

Le site fait lui-même le tri dans sa balise canonical : il **garde `page`**
(quand ≥ 2, sur une rubrique) et **jette tout le reste**. `?page=N` est la
seule voie d'accès à l'historique d'une rubrique (contrairement à rts.ch, la
pagination n'est pas interdite par robots.txt et va très loin : `?page=100`
sur `/economie` est encore une page pleine).

**Conclusion : la query est jetable sauf `page` (entier ≥ 2) sur une URL de
rubrique.** Ce n'est pas une variante d'article (aucun article multi-pages
observé), c'est une pagination de listing : porteuse pour le *crawl*
(découverte), inutile comme *entrée d'index* si l'on décide, comme pour
rts.ch, de n'indexer que la rubrique elle-même.

### Slash final

Les canonicals de rubrique sont **sans** slash (`https://www.letemps.ch/suisse`),
mais le serveur accepte les deux formes sans redirection : `/suisse/`,
`/suisse/geneve/` et `/suisse/plethore-candidats/` répondent 200 avec le même
contenu. La forme avec slash qu'ajoute `urlnorm` est donc fonctionnelle mais
non canonique.

## Question 2 — Rubrique vs article

### Inventaire sitemap (hors ligne)

`/sitemap.xml` = index de **343 sitemaps mensuels** (`/sitemap/AAAA-MM.xml`),
de 1998-03 à 2026-09. Six mois lus (1998-06, 2005-03, 2012-09, 2019-05,
2023-11, 2026-09) : 1 396 + 1 398 + 1 740 + 1 262 + 1 324 + 783 = **7 903 URLs**.

- Profondeur : **2 = 5 797, 3 = 2 106, aucune autre**. Zéro URL de profondeur 1.
- Extension : 1 seule URL avec `.html` (`/grands-formats/…-dexperts.html`),
  qui répond **404**. Les 7 902 autres sont sans point.
- Premiers segments (23 distincts) : `suisse` 1 743, `monde` 1 288,
  `economie` 1 271, `culture` 1 158, `opinions` 721, `sport` 529,
  `societe` 519, `sciences` 205, `cyber` 174, `carrieres-et-formation` 89,
  `videos` 46, `archive-import-drupal` 40, `gastronomie-vin` 37,
  `articles` 18, `heidi` 16, `en-images` 12, `immobilier` 11, `podcasts` 10,
  `dessin-de-la-semaine` 9, `contenus-partenaires` 3, `grands-formats` 2,
  `data` 1, `evenements` 1.
- **Profondeur 3 = `/rubrique/sous-rubrique/slug`** : le segment médian prend
  **63 valeurs distinctes** réparties sur 11 rubriques —
  `suisse/{berne, fribourg, geneve, jura, neuchatel, suisse-alemanique,
  tessin, valais, vaud}`, `monde/{afrique, ameriques, asie-oceanie, europe,
  france, moyenorient}`, `economie/{energie, entreprises, finance,
  horlogerie-joaillerie, innovation, pharmas-medtech}`, `culture/{arts,
  ecrans, livres, musiques, scenes}`, `opinions/{chroniques, debats,
  editoriaux, revues-de-presse}`, `societe/{egalite, enfants-education,
  sciences-humaines, styles}`, `sport/{alpinisme, cyclisme, football, hockey,
  ski-snowboard, tennis, voile}`, `sciences/{environnement, espace,
  physique-chimie, sante, sciences-de-la-vie}`, `cyber/{cybersecurite,
  intelligence-artificielle}`, `podcasts/{5 émissions}`, `videos/{10 séries}`.
  `podcasts` et `videos` n'existent **qu'**à profondeur 3 (10/10 et 46/46).
- Forme des feuilles : 7 899 slugs `mots-separes-par-tirets`, 2 `slug-idnum`,
  1 identifiant numérique pur (`/videos/visions-des-confins/1211473`).
  Longueur des slugs d'article : 5 à 196 caractères (médiane 46) — les
  articles de 1998 ont des slugs très courts (`/suisse/plethore-candidats`,
  `/suisse/detail-0`), ce qui interdit une règle par longueur.

Le sitemap ne contient donc **que des articles** ; les pages de listing n'y
figurent jamais et n'arrivent que par le crawl (navigation, `?page=N`).

### Vérification en direct (48 URLs)

Classement par le HTML : le JSON-LD est discriminant (`NewsArticle` sur les
articles, `Organization` seul sur les listings) ; `og:type` vaut `article`
partout et ne sert à rien. Contrôle croisé par le nombre de liens d'articles
dans `<main>` et la présence d'un lien `?page=`.

| profondeur | testées | LISTING | ARTICLE | autre |
|---|---|---|---|---|
| 1 | 20 | 17 (dont 15 paginées) | **0** | 3 pages statiques (`/a-propos`, `/impressum`, `/blogs`) |
| 2 | 25 | **11** | **10** | `/profil` 404, `/videos/visions-des-confins` 404 (série disparue, son épisode répond encore 200), `/heidi/<slug>` 308 → heidi.news, `…dexperts.html` 404 |
| 3 | 8 | 0 | **8** | — |

Détail de la profondeur 2 :

- **Listings (11)** : `/suisse/geneve`, `/suisse/berne`, `/economie/finance`,
  `/opinions/editoriaux`, `/opinions/chroniques`, `/monde/europe`,
  `/culture/livres` (sous-rubriques, 20-21 liens, toutes paginées),
  `/profil/luis-lema` (auteur, 22 liens, paginé), `/tags/ukraine` (20 liens,
  paginé), `/podcasts/raffut` (18 liens) et `/videos/actualite` (22 liens,
  paginé) — ces deux derniers portent un JSON-LD `NewsArticle` bien qu'ils
  soient des pages d'émission/série ; le compte de liens tranche.
- **Articles (10)** : `/suisse/plethore-candidats` (1998), `/suisse/detail-0`
  (1998), `/societe/geneve-futurs-bacheliers-…`, `/suisse/le-depart-surprise-…`,
  `/en-images/coulisses-forum-100`, `/archive-import-drupal/ueli-maurer-…`,
  `/articles/une-usine-foxconn-…`, `/dessin-de-la-semaine/le-dessin-de-kichka-…`,
  `/data/en-graphique-…`, `/evenements/claire-charmet-…` — tous `NewsArticle`,
  1 à 5 liens.
- Slugs : listings de **5 à 10** caractères testés (21 max dans le vocabulaire
  sitemap : `horlogerie-joaillerie`), articles de **8 à 120**. Recouvrement
  réel (`detail-0`, `plethore-candidats`) : la longueur seule ne suffit pas.

### Réponses

1. Profondeur 1 = listing ou page statique (0/20 article). Profondeur 3 =
   article (8/8 en direct, 2 106/2 106 dans le sitemap par construction).
   **Profondeur 2 est mixte** : 11 listings contre 10 articles dans
   l'échantillon.
2. La distinction n'est **pas** strictement liée à la profondeur : les
   sous-rubriques (`/suisse/geneve`), les auteurs (`/profil/<nom>`), les tags
   (`/tags/<mot>`), les émissions (`/podcasts/<x>`, `/videos/<x>`) sont des
   listings de niveau 2. Aucun article de niveau 1 observé. Un même préfixe
   sert aux deux : `/opinions/editoriaux` est un listing **et** le parent des
   lignes `/opinions/editoriaux/<slug>` — ce qui colle exactement au modèle
   « dossier + lignes » du mapping disque, à condition de classer juste.
3. Signal fiable depuis l'URL seule : le **vocabulaire** du second segment.
   Un chemin de profondeur 2 est un dossier si (a) son premier segment est
   un préfixe de listing pur (`profil`, `tags`, `podcasts`, `videos`,
   `dossiers`) ou (b) son second segment appartient à la liste des
   sous-rubriques de son premier segment — liste dérivable automatiquement :
   *tout préfixe de profondeur 2 d'une URL de profondeur 3 est un dossier*
   (63 entrées ici, et 7/7 vérifiées comme listings). Pas de date ni
   d'identifiant numérique dans les slugs (1 exception sur 7 903).
4. Voir le rapport heidi.news : même squelette, vocabulaires disjoints.

### Volume

343 mois × ~1 424 URLs/mois (moyenne des cinq mois complets lus) ≈
**490 000 URLs**, du même ordre que les ~459 000 lignes déjà indexées pour
rts.ch. Avec la règle actuelle, **chacune
deviendrait un dossier** (~490 000 dossiers + fichiers `_index` de 3 octets).
Avec la règle ci-dessous : ~23 rubriques + 63 sous-rubriques + quelques
centaines de pages auteur/tag/émission, soit **quelques centaines de
dossiers**, chacun portant des milliers de lignes (`suisse/` seule : ~100 000).
À noter pour le sharding : `/suisse/<slug>` (profondeur 2) concentre
1 743 / 7 903 = 22 % des URLs dans un seul `_index`.

## Brouillon de règles pour le profil

- **Query** : supprimer tout, sauf `page` quand la valeur est un entier ≥ 2
  **et** que le chemin est classé rubrique. Utiliser ces URLs pour la
  découverte (crawl de l'historique), sans les indexer comme entrées (la
  canonical de `/suisse?page=1` est `/suisse`).
- **Slash final** : forme canonique sans slash pour les rubriques ; le
  serveur tolère les deux, choisir une forme et s'y tenir.
- **Classification** (sans extension) :
  - profondeur 0 ou 1 ⇒ **dossier** ;
  - profondeur ≥ 3 ⇒ **ligne** dans le dossier parent ;
  - profondeur 2 ⇒ **dossier** si `seg1 ∈ {profil, tags, podcasts, videos,
    dossiers}` ou si `seg2 ∈ SOUS_RUBRIQUES[seg1]` (liste des 63 paires
    ci-dessus, à maintenir en ajoutant tout préfixe de profondeur 2 observé
    sur une URL de profondeur 3) ; sinon **ligne**.
- **Exclusions** : `/heidi/*` (308 vers heidi.news), `/compte/*`,
  `/cdn-cgi/*`, `/recherche`, plus les Disallow du robots.txt.
- **Débit** : 1 req/s sans incident sur 74 requêtes.
