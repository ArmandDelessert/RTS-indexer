# Moi

Contexte : je généralise le moteur du dépôt GitHub ArmandDelessert/RTS-indexer
(actuellement mono-site, rts.ch) pour qu'il puisse aussi indexer lemonde.fr,
letemps.ch et heidi.news. Une première sonde automatisée (script Python,
1 req/s, tirage aléatoire dans les sitemaps) a déjà tourné sur les trois sites
et produit un premier rapport (sondage/rapport.json sur la branche
claude/gifted-pascal-bsletn du dépôt, commit efcc46f). Elle a confirmé que
les trois sites sont indexables (pas de blocage robots.txt, pas de paywall
bloquant côté HTTP — tout répond 200) mais a laissé trois questions ouvertes,
faute d'avoir pu tester au-delà d'un tirage dans les sitemaps. Ce sont ces
trois questions que je veux trancher maintenant, avec des requêtes réelles en
direct plutôt qu'un script figé.

Avant de commencer : clone ou consulte le dépôt ArmandDelessert/RTS-indexer
(branche claude/gifted-pascal-bsletn) et lis en particulier
src/rts_indexer/urlnorm.py et src/rts_indexer/pathmap.py. C'est le code dont
dépendent les trois questions ci-dessous — elles n'ont de sens qu'à la
lumière de ce que ces deux modules font.

Règles de politesse à respecter pour toute requête, sur les trois sites :
- lire et respecter robots.txt à chaque fois (Disallow, Crawl-delay) ;
- au plus 1 requête par seconde par site, jamais en parallèle sur un même
  site ;
- User-Agent explicite et honnête, ex. "url-indexer-probe/0.1
  (+https://github.com/ArmandDelessert/RTS-indexer)" ;
- une vingtaine de requêtes par site suffit pour chaque test ci-dessous —
  pas de crawl de masse, ce n'est pas l'objectif.

## Question 1 — La query string est-elle porteuse ?

`urlnorm.normalize()` retire actuellement TOUTES les query strings de toute
URL, sans exception (c'est correct pour rts.ch : ses URLs canoniques n'en ont
jamais). Pour les trois nouveaux sites, il faut vérifier si un paramètre de
requête peut faire partie de l'identité canonique d'une page plutôt que d'être
cosmétique (tracking, pagination, ancre).

Pour chacun des trois sites :
1. Trouve une dizaine d'URLs *avec* query string réellement utilisées sur le
   site (regarde les liens internes d'une page d'accueil ou d'une page de
   rubrique — pas seulement le sitemap, qui n'en contient jamais).
2. Pour chacune, récupère la page avec la query string, puis la même URL sans
   la query string.
3. Compare : même statut HTTP ? Contenu substantiellement différent (pas
   juste un horodatage ou un jeton qui varie d'un appel à l'autre) ?
4. Conclusion par site : la query est-elle toujours jetable (comme rts.ch),
   ou existe-t-il des paramètres porteurs qu'il faudra explicitement
   conserver dans le profil de configuration (ex. pagination d'un
   article multi-pages, variante linguistique) ?

## Question 2 — Comment chaque site distingue une rubrique d'un article ?

C'est le point dur trouvé dans la première sonde. `pathmap.py` décide
« dossier vs ligne dans l'index du parent » sur une seule règle : le segment
terminal de l'URL contient-il un point (extension) ? Sans point → dossier
(rubrique). Avec un point HTML → ligne (article).

Cette règle fonctionne pour rts.ch et semble fonctionner pour lemonde.fr
(URLs en .html). Mais letemps.ch et heidi.news n'ont AUCUNE extension dans
leurs URLs (mesuré : 12433/12433 et 2041/2041 sans point sur le segment
terminal) — la règle actuelle traiterait donc CHAQUE ARTICLE comme un
dossier, ce qui serait un désastre de stockage (un dossier + un fichier
_index.txt de 3 octets par article, au lieu d'une ligne dans l'index de sa
rubrique).

Pour letemps.ch et heidi.news, détermine empiriquement :
1. Prends une vingtaine d'URLs de profondeur 1 (ex. [www.letemps.ch/suisse](https://www.letemps.ch/suisse))
   et une vingtaine de profondeur 2-3 (des articles présumés). Vérifie que
   les premières sont bien des pages de LISTING (liens vers plusieurs
   articles) et les secondes des pages d'ARTICLE individuel (un seul sujet).
2. Cette distinction est-elle strictement liée à la PROFONDEUR du chemin
   (rubrique = niveau 1, article = niveau 2+), ou y a-t-il des contre-exemples
   (sous-rubriques à niveau 2, articles à niveau 1, pages de listing profondes
   comme une page d'auteur ou un tag) ?
3. Existe-t-il un autre signal fiable et détectable depuis l'URL seule (sans
   téléchargement de la page) — un préfixe de chemin systématique
   (/opinions/, /blog/), un motif dans le slug (date, identifiant numérique) ?
   Le but : pouvoir écrire une règle de classification déclarative
   (par ex. "profondeur >= 2 => article" ou "le premier segment appartient à
   telle liste connue => rubrique, sinon article") plutôt que par extension.
4. Vérifie si letemps.ch et heidi.news (même plateforme technique d'après
   leur robots.txt identique) partagent exactement la même règle, ou si elles
   divergent malgré leur robots.txt commun.

## Question 3 — Quelle est l'ampleur réelle de lemonde.fr ?

La première sonde s'est arrêtée à 3983 URLs faute de budget (12 documents
sitemap téléchargés). Le Monde semble découper ses URLs par
section/article/année/mois/jour (profondeur médiane 6, max 7 observés sur cet
échantillon partiel) — beaucoup plus profond que rts.ch (5 segments max).

1. Explore la structure complète des sitemaps de lemonde.fr (sitemap_index.xml
   déclaré dans robots.txt) : combien de sitemaps enfants, sur quelle période
   couvrent-ils, combien d'URLs au total si on extrapole ?
2. Confirme que TOUTES les URLs suivent le même gabarit
   /section/article/AAAA/MM/JJ/slug_id1_id2.html, ou repère les exceptions
   (pages spéciales, lives, podcasts, vidéos, contenu payant avec un gabarit
   différent).
3. Avec le nombre de sections distinctes observées et une fourchette d'années
   couvertes, estime l'ordre de grandeur du nombre de dossiers
   "section/article/AAAA/MM/JJ" que produirait le mapping disque actuel — et
   compare-le aux 5148 dossiers actuels de rts.ch pour donner une idée du
   changement d'échelle.
4. Teste aussi, en une vingtaine de requêtes, si un anti-bot se déclenche à
   un rythme un peu plus soutenu qu'un simple tirage aléatoire (ex. cinq
   requêtes consécutives sur le même sous-domaine à 1 req/s) — la première
   sonde n'a testé que 30 requêtes isolées et n'a rien vu, ce qui ne prouve
   pas grand-chose.

## Rendu attendu

Un rapport texte, un par site, qui répond aux trois questions avec les
chiffres à l'appui (pas juste une conclusion qualitative). Pour les questions
1 et 2, conclus si possible par un brouillon des règles déclaratives
correspondantes (quels paramètres de query garder le cas échéant, quelle
règle de classification rubrique/article) — sans écrire de code, juste la
règle en clair, je m'occuperai de l'intégrer au format de profil du moteur
généralisé ensuite.

# Claude

Je commence par lire le code dont dépendent les trois questions.

Le commit efcc46f est déjà sur `main` ici (la branche `claude/gifted-pascal-bsletn` a été fusionnée). Je lis les deux modules et le rapport de la première sonde.

Je mets en place un client poli dans le scratchpad (vérification robots.txt, 1 req/s par hôte, user-agent explicite, cache disque pour éviter les re-téléchargements), puis j'enchaîne les expériences.

Robots.txt lus. Étape 1 : collecte des liens internes **avec query string** sur les pages d'accueil et quelques pages de rubrique (question 1).

Les liens avec query sont peu nombreux et relèvent de trois familles : tracking (lmd_*, utm_*, srsltid, promo_interne, from_newsletter), pagination de listing (page=N) et navigation applicative (faq/?question=, preferred_lang). Je compare maintenant chaque URL avec et sans query.

Le Monde utilise la pagination par chemin (`/international/2`) plutôt que par query string, même si `?page=2` fonctionne aussi. Je vérifie cette équivalence avant de passer à letemps.ch et heidi.news.

Question 1 est réglée pour les trois sites. Je passe à la question 2 en récupérant d'abord les sitemaps de letemps.ch et heidi.news pour analyser la structure des URLs hors ligne avant de lancer des requêtes de vérification.

Je constate que les URLs de profondeur 3 comportent souvent une sous-rubrique ou une série en position médiane, ce qui remet en cause la règle « profondeur ≥ 2 ⇒ article ». J'extrais maintenant les liens de navigation des pages déjà en cache pour construire l'échantillon de test.

Je démarre la vérification en direct sur environ 20 URLs de profondeur 1 et 20 de profondeur 2-3 par site, en les classant LISTING/ARTICLE selon des indices HTML comme og:type, JSON-LD, liens d'articles dans <main> et présence d'une date de publication.

Je corrige mon verdict : `og:type=article` n'est pas fiable sur letemps.ch, mais le JSON-LD (`NewsArticle` vs `Organization`) permet une discrimination fiable, sans nouvelle requête. Je lance maintenant l'analyse de heidi.news.

heidi.news a déclenché un 429 en série au milieu du lot. Je regarde la chronologie et les en-têtes avant de reprendre.

heidi.news a déclenché un blocage 429 après 22 requêtes en 45 s (nginx, ~15 s de blocage puis retour à 200), tandis que letemps.ch sur la même plateforme n'a rien bloqué en 45 requêtes. Je relance les 18 URLs manquantes de heidi à un rythme d'1 requête/3 s pour identifier le seuil exact.

À 1 req/3 s, j'ai testé 18 requêtes sur heidi sans aucun 429. Je complète maintenant letemps.ch avec trois candidats listing hors rubriques classiques et je mesure le vocabulaire des sous-rubriques.

Je remarque que les URLs canoniques de rubrique n'ont pas de slash final, contrairement à ce que `urlnorm` ajoute automatiquement. Je vais tester le comportement du serveur avec un slash final pour vérifier ce point.

Q2 est instruite. Question 3 : structure complète des sitemaps de lemonde.fr.

J'ai identifié deux familles de sitemaps parallèles : hebdomadaire (869 fichiers, tous des lundis) et mensuelle (982 fichiers couvrant 1944-12 à 2026-09), plus les sitemaps videos, images et 11 spéciaux. Je vais maintenant échantillonner des fichiers mensuels à travers les décennies pour estimer le volume.

Je confirme que le mensuel constitue bien l'ensemble complet, le hebdo n'étant qu'un sous-ensemble (784 + 801 URLs pour deux semaines de mars 2024 contre 3450 pour le mois entier). Je passe maintenant à l'analyse hors ligne des gabarits d'URL sur les ~53 000 URLs récupérées.

`e.fr` était un artefact de mon découpage, correspondant en fait à des URLs sur `podcasts.lemonde.fr`. Les vraies exceptions sur www.lemonde.fr sont les blogs, avec un format spécifique en slash final sans `.html` — je passe maintenant au calcul de l'extrapolation volume/dossiers.

Le journal montre déjà 17 sitemaps consécutifs traités à 1 req/s sans incident. Je lance maintenant une rafale ciblée de 20 pages d'articles (dont 5 dans la même section) en surveillant l'apparition de marqueurs anti-bot comme Datadome ou un captcha.

J'ai obtenu 20 succès sur 20 requêtes en 200, 19 secondes, dont 5 consécutives sur `/international/` — pas de trace d'anti-bot sur lemonde.fr. Je vérifie maintenant si les niveaux intermédiaires du gabarit d'URL existent comme pages à part entière, pour finaliser le mapping disque.

J'ai les 235 requêtes conformes au robots.txt (lemonde 75, letemps 74, heidi 86, toutes à ≤ 1 req/s). Je rédige maintenant les trois rapports dans `sondage/`, aux côtés du `rapport.json` existant.

Correction d'échelle : rts.ch compte en réalité ~459 000 lignes indexées (le « 24 000 » du commentaire de `urlnorm.py` est périmé). Je rectifie les comparaisons.

Les trois rapports sont écrits dans `sondage/` (non commités), à côté du `rapport.json` de la première sonde :

- [rapport-2-lemonde.fr.md](sondage/rapport-2-lemonde.fr.md)
- [rapport-2-letemps.ch.md](sondage/rapport-2-letemps.ch.md)
- [rapport-2-heidi.news.md](sondage/rapport-2-heidi.news.md)

Méthode : 235 requêtes en direct (lemonde 75, letemps 74, heidi 86), robots.txt relu avant chaque requête, 1 req/s max par hôte, jamais en parallèle, UA `url-indexer-probe/0.1 (+https://github.com/ArmandDelessert/RTS-indexer)`. Le journal complet et les pages sont dans le scratchpad.

## L'essentiel

**Q1 — Query string.**
- **lemonde.fr : toujours jetable**, comme rts.ch. 41 liens à query trouvés, 14 paires comparées : tout est du tracking (`lmd_*`, `utm_*`, `srsltid`) ou de la navigation JS sans effet serveur. Le seul paramètre qui change le contenu, `?page=N`, est un alias non lié de la pagination officielle **par chemin** (`/international/2`, identique à 100 %).
- **letemps.ch et heidi.news : jetable sauf `page` (entier ≥ 2) sur une rubrique.** Le site lui-même garde `page` dans sa canonical et jette le reste (`/suisse?page=2&utm_source=x` → canonical `/suisse?page=2`). Ratio texte 0.07-0.32 entre page N et page 1. Ce n'est pas un article multi-pages mais l'unique accès à l'historique d'une rubrique (`/economie?page=100`, `/climat?page=65` encore pleines) — porteuse pour le crawl, pas comme entrée d'index.

**Q2 — Rubrique vs article (letemps/heidi).** Les sitemaps ne contiennent que des articles (profondeur 2 ou 3, 100 %). En direct (48 + 44 URLs) : profondeur 1 = listing ou page statique (0/40 article), profondeur 3 = article (12/12), **profondeur 2 = mixte** (letemps 11 listings / 10 articles, heidi 10 / 10). La profondeur ne suffit donc pas ; la longueur du slug non plus (`/suisse/detail-0` est un article de 1998). Signal fiable : le **vocabulaire du second segment**. Règle proposée :

> profondeur 0-1 ⇒ dossier ; ≥ 3 ⇒ ligne ; profondeur 2 ⇒ dossier si `seg1 ∈ {profil, tags, podcasts, videos, …}` ou si `seg2 ∈ SOUS_RUBRIQUES[seg1]`, sinon ligne.

Divergence malgré la plateforme commune : sur letemps la liste des 63 sous-rubriques se **dérive automatiquement** (tout préfixe d'une URL de profondeur 3), sur heidi elle doit être **déclarée** (`articles/{analyse, editorial, …}`, `climat/{energie, climat-1}` n'ont aucun article sous eux). Et heidi bloque en **429 au-delà de ~20 req/min** (nginx, « Retry later »), zéro 429 à 1 req/3 s ; letemps (Cloudflare) n'a rien bloqué.

**Q3 — lemonde.fr.** 3 296 sitemaps enfants ; la famille mensuelle `year/articles` (982 fichiers, 1944-12 → 2026-09) est complète, l'hebdo en est un sous-ensemble. 11 mois lus → **≈ 3,4 M d'URLs** (+ `/en/`, vidéos, 35 000 pages de résultats pour un seul scrutin). Gabarit `/section/type/AAAA/MM/JJ/slug_id1_id2.html` respecté à **99.35 %** ; les exceptions sont les blogs (`/<blog>/AAAA/MM/JJ/slug/`, slash final, deviendraient des dossiers) et la pagination `/section/2` (dont la forme avec slash répond 404). Dossiers `section/type/AAAA/MM/JJ` extrapolés : **≈ 279 000 (~300 000 avec les niveaux supérieurs) contre 5 148 pour rts.ch**, 3-4 articles par dossier. Anti-bot : 20 pages en 19 s dont 5 consécutives sur `/international/` → 20/20 en 200 ; 17 sitemaps de 1-5 Mo à 1 req/s → 17/17.

Deux points que je n'avais pas anticipés et qui touchent le moteur :
- **Le Monde re-date ses articles** : 3 URLs sitemap testées ont répondu 301 vers une autre date (`2025/03/10` → `2026/06/29`), seul `id1` est stable — sans déduplication sur cet identifiant, un même article finira dans plusieurs dossiers.
- **Le « 24 000 URLs » du commentaire de `urlnorm.py` est périmé** : `data/` contient ~459 000 lignes ; letemps (~490 000) est donc du même ordre que rts.ch, heidi ~2 %, lemonde 7-8 ×.
