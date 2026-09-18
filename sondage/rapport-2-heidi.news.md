# Sonde n°2 — www.heidi.news

Requêtes en direct du 18 septembre 2026, User-Agent
`url-indexer-probe/0.1 (+https://github.com/ArmandDelessert/RTS-indexer)`,
robots.txt relu et appliqué (les 7 mêmes Disallow que letemps.ch, pas de
Crawl-delay, pas de Sitemap déclaré), 1 requête/s au plus (puis 1 req/3 s
après le blocage décrit plus bas), jamais en parallèle. **86 requêtes** :
68 × 200, **17 × 429**, 1 × 404 (`/sitemap_index.xml`, sondé à l'aveugle).

## Limitation de débit (mesurée, à lire avant le reste)

- À **1 req/s** : 22 requêtes en 45 s → **429** à partir de la 23ᵉ, puis 429
  sur les 16 requêtes suivantes (16 s), puis retour à 200 (les 5 requêtes
  suivantes). Réponse : `Server: nginx`, `Content-Type: text/plain`, corps
  « Retry later », **pas de `Retry-After`**, 0.04 s de latence (rejet en
  bordure, la page n'est pas calculée).
- À **1 req/3 s** : 24 requêtes, **zéro 429**.
- Première sonde : 10 × 429 sur 30 requêtes isolées.

Tout est cohérent avec un quota d'environ **20 requêtes par minute glissante**.
letemps.ch (même robots.txt, même moteur) n'a rien bloqué en 74 requêtes à
1 req/s : la limite est propre à heidi.news (nginx nu, sans Cloudflare devant).
Réglage à retenir pour le profil : **≥ 3 s entre deux requêtes**, et traiter
un 429 comme une pause de 60 s, pas comme une URL morte.

## Question 1 — La query string est-elle porteuse ?

### Liens à query réellement utilisés

Liens internes de `/`, `/sante` et `/climat` : **8 URLs distinctes** avec
query, trois paramètres :

| paramètre | occ. | exemple | nature |
|---|---|---|---|
| `promo_interne` | 9 | `/abonnements?promo_interne=nav_bouton` | tracking |
| `page` | 8 | `/sante?page=2`, `?page=3`, `/climat?page=65` | **pagination des rubriques** |
| `from_newsletter` | 6 | `/compte/sign_up?from_newsletter=true` | tracking |

Aucun lien d'article n'a de query ; aucune URL sitemap non plus (573 lues).

### Comparaison avec / sans query (10 paires)

| URL testée | statuts | ratio texte | Jaccard liens | canonical de la page avec query |
|---|---|---|---|---|
| `/sante?page=2` vs `/sante` | 200/200 | **0.19** | 0.07 | `/sante?page=2` |
| `/sante?page=3` | 200/200 | **0.21** | 0.05 | `/sante?page=3` |
| `/climat?page=2` | 200/200 | **0.32** | 0.14 | `/climat?page=2` |
| `/climat?page=65` | 200/200 | **0.07** | 0.08 | `/climat?page=65` (dernière page, 23 liens au lieu de 32) |
| `/sante?page=1` | 200/200 | 1.00 | 1.00 | `/sante` |
| `/?page=2` vs `/` | 200/200 | 1.00 | 1.00 | `/?page=2` (canonical incohérente, contenu identique) |
| `/sante?utm_source=test` | 200/200 | 1.00 | 0.83 | `/sante` |
| `/climat?page=2&promo_interne=x` | 200/200 | 0.32 | 0.10 | `/climat?page=2` |
| `/abonnements?promo_interne=nav_bouton` | 200/200 | 1.00 | 1.00 | `/abonnements/edition-france` |
| `/compte/sign_up?from_newsletter=true` | 200/200 | 1.00 | 1.00 | `/compte/sign_up` |

Même comportement que letemps.ch : le site garde `page` (≥ 2, sur une
rubrique) dans sa canonical et jette tout le reste. La pagination va loin
(`/climat?page=65` est la dernière page, avec 65 × ~24 ≈ 1 500 articles pour
la seule rubrique climat).

**Conclusion : query jetable sauf `page` (entier ≥ 2) sur une rubrique**,
porteuse pour la découverte, pas pour l'index.

### Slash final

Comme letemps.ch : canonicals sans slash, mais `/sante/`, `/climat/energie/`
et `/suisse/f-35-obeir-dispense-t-il-de-reflechir/` répondent 200 avec le
même contenu, sans redirection.

## Question 2 — Rubrique vs article

### Inventaire sitemap (hors ligne)

`/sitemap.xml` = index de **92 sitemaps mensuels** (`/sitemap/AAAA-MM.xml`)
de 2019-01 à 2026-09. Cinq mois lus (2019-06, 2021-03, 2023-09, 2025-05,
2026-09) : 220 + 259 + 37 + 35 + 22 = **573 URLs**. Le rythme de publication
a été divisé par ~6 entre 2019-2021 et 2023-2026.

- Profondeur : **2 = 486, 3 = 87, aucune autre**. Zéro `.` dans les feuilles.
- Premiers segments (15 distincts) : `sciences` 229, `sante` 109,
  `explorations` 85, `solutions` 36, `climat` 27, `culture` 17,
  `articles` 16, `monde` 14, `cyber` 12, `education` 12, `suisse` 7,
  `ca-pourrait-vous-etonner` 4, `videos` 2, `alimentation` 2, `evenements` 1.
- **Profondeur 3 = `/explorations/<série>/<épisode>` (85) et
  `/videos/<série>/<épisode>` (2)**, rien d'autre : 25 séries d'explorations
  distinctes (`pierre-maudet-la-campagne-de-la-derniere-chance` 9 épisodes,
  `a-geneve-de-la-curatelle-au-cauchemar` 8, `la-revolution-des-toilettes`
  7…). `explorations` et `videos` n'existent **jamais** à profondeur 2 dans
  le sitemap.
- Contrairement à letemps.ch, **aucune sous-rubrique thématique n'apparaît
  comme segment médian** : les articles de `/climat/energie` (listing vérifié
  en direct) vivent à `/climat/<slug>` ou `/sciences/<slug>`, pas à
  `/climat/energie/<slug>`.
- Slugs d'article : 12 à 140 caractères, médiane 68, tous en
  `mots-separes-par-tirets` ; pas de date, pas d'identifiant numérique.

Le sitemap ne contient que des articles.

### Vérification en direct (44 URLs)

Même classement que pour letemps.ch (JSON-LD `NewsArticle` vs
`Organization`, liens d'articles dans `<main>`, lien `?page=`).

| profondeur | testées | LISTING | ARTICLE | autre |
|---|---|---|---|---|
| 1 | 20 | 15 (13 paginées ; `/explorations` 154 liens, `/auteurs` 8) | **0** | `/medias`, `/a-propos`, `/faq` statiques ; `/videos` 7 liens ; **`/tags` → 200 avec corps vide (0 octet)** |
| 2 | 20 | **10** | **10** | — |
| 3 | 4 | 0 | **4** | — |

Détail de la profondeur 2 :

- **Listings (10)** : `/articles/analyse` (30 liens, paginé),
  `/articles/editorial` (35, paginé), `/articles/podcast` (14) — listings
  **par format** ; `/climat/energie` (8), `/climat/climat-1` (24, paginé) —
  sous-rubriques thématiques ; `/explorations/41-secondes` (10),
  `/explorations/rendez-vous-en-terre-atomique` (7) — pages de série ;
  `/tags/reensauvagement` (22, paginé) ; `/profil/kaveh-omidvar` (8) ;
  `/videos/popscience` (10, canonical → `/explorations/popscience`).
- **Articles (10)** : `/sciences/la-constellation-starlink-…`,
  `/sante/le-dessin-de-la-semaine-…`, `/suisse/f-35-obeir-…`,
  `/monde/25-ans-apres-le-11-septembre-…`, `/cyber/l-armee-se-prepare-…`,
  `/articles/comment-ajouter-heidi-news-…`, `/articles/la-medecine-souffre-…`,
  `/ca-pourrait-vous-etonner/on-n-a-vraiment-…`,
  `/alimentation/les-petits-repor-terres-…`, `/evenements/masterclass-…` —
  tous `NewsArticle`, 1 à 2 liens.
- Slugs : listings de **7 à 15** caractères, articles de **37 à 86** (12 min
  dans le sitemap). Ici la longueur séparerait, mais `/articles/<format>`
  contre `/articles/<slug>` montre qu'un même préfixe porte les deux natures.

### Réponses

1. Profondeur 1 = listing ou page statique (0/20 article). Profondeur 3 =
   article (4/4, 87/87 dans le sitemap). **Profondeur 2 mixte** : 10 / 10.
2. Pas strictement liée à la profondeur : formats (`/articles/<format>`),
   sous-rubriques (`/climat/energie`), séries (`/explorations/<série>`),
   tags, auteurs sont des listings de niveau 2. Aucun article de niveau 1.
3. Signal depuis l'URL seule : le vocabulaire, comme pour letemps.ch, mais
   avec une différence de taille — la dérivation automatique « préfixe d'une
   URL de profondeur 3 ⇒ dossier » ne trouve que `explorations/*` et
   `videos/*` ; **les sous-rubriques thématiques et les formats doivent être
   déclarés explicitement** (ou extraits de la navigation de la rubrique
   parente), car aucune URL plus profonde ne les révèle. Liste observée :
   `articles/{analyse, chronique, editorial, enquête, interview, news,
   opinions, podcast, video}` (attention : `enquête` est lié
   percent-encodé `enqu%C3%AAte`), `climat/{energie, climat-1}`.
4. **Même squelette, règles divergentes** :

| | letemps.ch | heidi.news |
|---|---|---|
| profondeur 1 / 3 | listing / article | listing / article |
| sous-rubriques thématiques | 63, avec articles à profondeur 3 (`/economie/finance/<slug>`) | quelques-unes, **sans** article à profondeur 3 |
| profondeur 3 | `/rubrique/sous-rubrique/<slug>` + émissions | uniquement `/explorations|videos/<série>/<épisode>` |
| `/articles/<x>` | toujours article | format (listing) **ou** article |
| auteurs | `/profil/<nom>` (index `/profil` 404) | `/profil/<nom>` + index `/auteurs` |
| `/tags` | `/tags/<mot>` listing | `/tags/<mot>` listing ; `/tags` = 200 vide |
| débit | 1 req/s sans incident (Cloudflare) | 429 au-delà de ~20 req/min (nginx) |
| passerelle | `/heidi/<slug>` → 308 vers heidi.news `/explorations/tous-les-trente-jours/<slug>` | — |

### Volume

92 mois ; ~240 URLs/mois en 2019-2021 (~35 mois), ~35/mois depuis 2023
(~57 mois) ⇒ **≈ 10 000 à 12 000 URLs** au total, ~2 % des ~459 000 lignes de rts.ch.
Avec la règle actuelle : ~11 000 dossiers d'un article ; avec la règle
ci-dessous : ~15 rubriques + ~10 sous-rubriques/formats + ~30 séries + les
pages auteur/tag, soit **moins de 100 dossiers**.

## Brouillon de règles pour le profil

- **Query** : supprimer tout, sauf `page` (entier ≥ 2) sur une URL classée
  rubrique — pour la découverte uniquement.
- **Slash final** : forme canonique sans slash (le serveur tolère les deux).
- **Classification** (sans extension) :
  - profondeur 0 ou 1 ⇒ **dossier** ;
  - profondeur ≥ 3 ⇒ **ligne** ;
  - profondeur 2 ⇒ **dossier** si `seg1 ∈ {explorations, videos, tags,
    profil}` (toujours listing à ce niveau) ou si `seg2 ∈
    SOUS_RUBRIQUES[seg1]` avec la liste **déclarée** :
    `articles → {analyse, chronique, editorial, enquête, interview, news,
    opinions, podcast, video}`, `climat → {energie, climat-1}` (à compléter
    depuis la navigation des autres rubriques) ; sinon **ligne**.
- **Exclusions** : `/compte/*`, `/tags` (racine vide), `/abonnements/*`,
  `/shop`, plus les Disallow du robots.txt.
- **Débit** : **1 requête toutes les 3 s minimum**, 429 ⇒ pause 60 s ;
  privilégier le sitemap (92 fichiers) au crawl.
