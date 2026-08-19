"""Interface en ligne de commande."""

from __future__ import annotations

import argparse
import logging
import shlex
import sys
import time
from pathlib import Path

from . import config, explorer, urlnorm
from . import verify as verify_module
from .sources import commoncrawl, fichier, rss, sitemap, wayback
from .sources import crawl as crawl_source
from .store import Store

log = logging.getLogger(__name__)

#: Instant de démarrage, posé par main(). `monotonic` et non `time()` : insensible
#: à un changement d'heure système (passage à l'heure d'hiver, resynchronisation
#: NTP) au milieu d'un run qui peut durer des heures.
_DEBUT: float | None = None


def _store(args: argparse.Namespace) -> Store:
    return Store(Path(args.data_dir))


def _log_setup(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )


def _duree(secondes: float) -> str:
    """Durée lisible : `2 h 05 min`, `4 min 12 s`, `8.3 s`."""
    if secondes < 60:
        return f"{secondes:.1f} s"
    minutes, sec = divmod(int(secondes), 60)
    if minutes < 60:
        return f"{minutes} min {sec:02d} s"
    heures, minutes = divmod(minutes, 60)
    return f"{heures} h {minutes:02d} min"


def _report(stats: dict[str, int]) -> None:
    """Affiche les compteurs, puis la durée écoulée depuis le lancement.

    La durée est imprimée à part et n'entre jamais dans ``stats`` : ce
    dictionnaire est celui que le store sérialise dans ``_stats.json``, et y
    glisser une valeur qui change à chaque exécution produirait un diff Git à
    chaque run.
    """
    width = max(len(k) for k in stats)
    for key, value in stats.items():
        print(f"{key.replace('_', ' '):<{width}}  {value:>9,}".replace(",", "'"))
    if _DEBUT is not None:
        print(f"{'duree':<{width}}  {_duree(time.monotonic() - _DEBUT):>9}")


def cmd_sitemap(args: argparse.Namespace, store: Store | None = None) -> int:
    urls = sitemap.collect()
    print(f"{len(urls)} URLs canoniques collectées depuis les sitemaps")
    if args.dry_run:
        for url in urls[: args.limit]:
            print(f"  {url}")
        return 0
    proprietaire = store is None
    store = store if store is not None else _store(args).load()
    added = store.add_many(urls)
    print(f"{added} nouvelles")
    if proprietaire:
        _report(store.write())
    return 0


def cmd_rss(args: argparse.Namespace, store: Store | None = None) -> int:
    """Collecte les articles fraîchement publiés depuis les flux RSS."""
    urls = rss.collect()
    print(f"{len(urls)} URLs collectées depuis {len(rss.feed_urls())} flux RSS")
    if args.dry_run:
        for url in urls[: args.limit]:
            print(f"  {url}")
        return 0
    proprietaire = store is None
    store = store if store is not None else _store(args).load()
    added = store.add_many(urls)
    print(f"{added} nouvelles")
    if proprietaire:
        _report(store.write())
    return 0


def cmd_crawl(args: argparse.Namespace, store: Store | None = None) -> int:
    """Parcourt les rubriques déjà connues pour en extraire les articles."""
    proprietaire = store is None
    store = store if store is not None else _store(args).load()
    known = [url for url, _ in store.urls() if url.endswith("/")]
    if not known:
        print(
            "Aucune rubrique dans l'index. Lancer d'abord: python -m rts_indexer sitemap",
            file=sys.stderr,
        )
        return 1
    seeds = crawl_source.select_seeds(known, limit=args.max_pages, reset=args.reset)
    print(
        f"{len(seeds)}/{len(known)} rubriques en graine cette exécution "
        f"(rotation), budget {args.max_pages} pages"
    )

    try:
        crawler = crawl_source.crawl(
            store,
            seeds,
            max_pages=args.max_pages,
            include_articles=args.include_articles,
        )
    except KeyboardInterrupt:
        # Une interruption volontaire (Ctrl+C) n'est pas une erreur : pas de
        # trace complète, juste la confirmation que rien n'est perdu. Écrit
        # immédiatement même au sein d'un `run` : au moment où on sait qu'on
        # s'arrête, mieux vaut sauver ce qui existe que respecter à la lettre
        # la règle « un seul write en fin de chaîne ».
        print("\nInterruption : écriture des URLs déjà découvertes...", file=sys.stderr)
        _report(store.write())
        raise
    except Exception:
        # Une erreur imprévue, elle, ne doit pas faire perdre les pages déjà
        # découvertes non plus : on écrit ce qui a été accumulé avant de
        # propager, mais avec la trace complète pour pouvoir diagnostiquer.
        log.exception("le crawl s'est interrompu, écriture des URLs déjà découvertes")
        _report(store.write())
        raise
    print(
        f"{crawler.fetched} pages visitées "
        f"({crawler.from_cache} inchangées), {crawler.discovered} URLs nouvelles"
    )
    if proprietaire:
        _report(store.write())
    return 0


def cmd_wayback(args: argparse.Namespace, store: Store | None = None) -> int:
    """Collecte l'archive historique via l'API CDX d'Internet Archive."""
    proprietaire = store is None
    store = store if store is not None else _store(args).load()
    try:
        client = wayback.collect(store, max_pages=args.max_pages, reset=args.reset)
    except KeyboardInterrupt:
        print("\nInterruption : écriture des URLs déjà collectées...", file=sys.stderr)
        _report(store.write())
        raise
    except Exception:
        log.exception("la collecte s'est interrompue, écriture des URLs obtenues")
        _report(store.write())
        raise

    print(
        f"{client.pages_fetched} pages CDX, {client.rows_seen} lignes brutes, "
        f"{getattr(client, 'added', 0)} URLs nouvelles"
    )
    if proprietaire:
        _report(store.write())
    return 0


def cmd_commoncrawl(args: argparse.Namespace, store: Store | None = None) -> int:
    """Collecte l'archive de la fondation Common Crawl."""
    proprietaire = store is None
    store = store if store is not None else _store(args).load()
    try:
        client = commoncrawl.collect(
            store,
            max_pages=args.max_pages,
            pages_per_index=args.pages_per_index,
            max_indexes=args.max_indexes,
            reset=args.reset,
        )
    except KeyboardInterrupt:
        print("\nInterruption : écriture des URLs déjà collectées...", file=sys.stderr)
        _report(store.write())
        raise
    except Exception:
        log.exception("la collecte s'est interrompue, écriture des URLs obtenues")
        _report(store.write())
        raise

    print(
        f"{client.pages_fetched} pages CDX, {client.rows_seen} lignes brutes, "
        f"{client.added} URLs nouvelles"
    )
    if proprietaire:
        _report(store.write())
    return 0


def cmd_verify(args: argparse.Namespace, store: Store | None = None) -> int:
    """Contrôle la vivacité des URLs indexées et met à jour le sigil `!`."""
    proprietaire = store is None
    store = store if store is not None else _store(args).load()
    if not store.dirs:
        print("Index vide. Lancer d'abord: python -m rts_indexer sitemap", file=sys.stderr)
        return 1

    try:
        verifier = verify_module.verify(
            store,
            max_urls=args.limit,
            recheck_days=args.recheck_days,
            path_prefix=args.path,
            dead_only=args.dead_only,
        )
    except KeyboardInterrupt:
        print("\nInterruption : écriture des verdicts déjà obtenus...", file=sys.stderr)
        _report(store.write())
        raise
    except Exception:
        log.exception("le contrôle s'est interrompu, écriture des verdicts obtenus")
        _report(store.write())
        raise

    print(
        f"{verifier.checked} URLs contrôlées : "
        f"{verifier.morts} nouvellement mortes, "
        f"{verifier.ressuscites} de nouveau vivantes, "
        f"{verifier.non_concluants} non concluantes "
        f"({verifier.reessais} seconds avis)"
    )
    if verifier.redirections:
        print(
            f"{len(verifier.redirections)} URLs redirigent ailleurs "
            f"(doublons probables, journalisés dans {config.ANOMALIES_FILE})"
        )
    if proprietaire:
        _report(store.write())
    return 0


def cmd_dedupe(args: argparse.Namespace, store: Store | None = None) -> int:
    """Supprime les URLs journalisées comme doublons par `verify`."""
    proprietaire = store is None
    store = store if store is not None else _store(args).load()
    supprimes, ajoutees, ignores = store.resolve_doublons()
    print(
        f"{supprimes} doublons supprimés ({ajoutees} cibles nouvellement indexées), "
        f"{ignores} requalifiés hors périmètre (pas de vrai doublon)"
    )
    if proprietaire:
        _report(store.write())
    return 0


def cmd_import(args: argparse.Namespace, store: Store | None = None) -> int:
    """Ajoute à l'index des URLs listées dans un fichier texte."""
    retenues, rejetees = fichier.lire(args.fichier)
    print(f"{len(retenues)} URLs retenues, {len(rejetees)} rejetées (hors périmètre ou malformées)")
    for ligne in rejetees[: args.limit]:
        print(f"  rejetée: {ligne}")

    a_ajouter = retenues
    if args.check and retenues:
        print(f"contrôle de {len(retenues)} URLs...")
        verdicts = verify_module.check_urls(retenues)
        a_ajouter, mortes, redirigees, douteuses = _trier_verdicts(retenues, verdicts)
        for url, cible in redirigees:
            print(f"  redirige: {url}\n         -> {cible}")
        for url, code in mortes:
            print(f"  morte (HTTP {code}): {url}")
        for url, code in douteuses:
            print(f"  non concluant (HTTP {code}): {url}")
        print(
            f"{len(a_ajouter)} vivantes, {len(redirigees)} redirigées (cible retenue à la place), "
            f"{len(mortes)} mortes écartées, {len(douteuses)} non concluantes écartées"
        )

    if args.dry_run:
        for url in a_ajouter[: args.limit]:
            print(f"  {url}")
        print("(--dry-run : rien n'a été écrit)")
        return 0

    proprietaire = store is None
    store = store if store is not None else _store(args).load()
    ajoutees = store.add_many(a_ajouter)
    print(f"{ajoutees} nouvelles URLs dans l'index ({len(a_ajouter) - ajoutees} déjà connues)")
    if proprietaire:
        _report(store.write())
    return 0


def _trier_verdicts(
    urls: list[str], verdicts: dict[str, verify_module.Verdict]
) -> tuple[list[str], list[tuple[str, int]], list[tuple[str, str]], list[tuple[str, int | None]]]:
    """Range chaque URL contrôlée selon son sort : à ajouter, morte, redirigée
    (c'est la cible qui est retenue), ou non concluante.

    Une redirection fait indexer la **cible** plutôt que l'URL demandée : c'est
    exactement le cas doublon que `verify` détecte déjà sur l'index existant,
    autant ne pas l'y introduire en premier lieu. La cible repasse par le
    filtre de périmètre, comme dans `resolve_doublons`.

    Même prudence qu'ailleurs : seuls 404/410 valent « morte ». Un 403, un
    5xx ou un échec réseau sont non concluants, donc écartés de l'ajout sans
    être déclarés morts — on ne sait simplement pas, et une liste tapée à la
    main mérite d'être resoumise plutôt que tranchée à tort.
    """
    vivantes: dict[str, None] = {}
    mortes: list[tuple[str, int]] = []
    redirigees: list[tuple[str, str]] = []
    douteuses: list[tuple[str, int | None]] = []

    for url in urls:
        code, finale = verdicts.get(url, (None, None))
        if code in config.VERIFY_DEAD_CODES:
            mortes.append((url, code))
        elif code is None or code >= 400:
            douteuses.append((url, code))
        elif finale and finale.rstrip("/") != url.rstrip("/"):
            cible = urlnorm.normalize(finale)
            if cible is None:
                # Redirige hors périmètre (image, domaine tiers) : ni l'URL
                # demandée ni sa cible n'ont leur place dans l'index.
                douteuses.append((url, code))
            else:
                redirigees.append((url, cible))
                vivantes.setdefault(cible, None)
        else:
            vivantes.setdefault(url, None)

    return list(vivantes), mortes, redirigees, douteuses


def cmd_purge(args: argparse.Namespace, store: Store | None = None) -> int:
    """Supprime de l'index les URLs actuellement marquées mortes.

    Par défaut, une URL morte reste indexée (sigil `!`) — cette commande
    n'est là que pour qui préfère explicitement un index de contenu vivant.
    Ne recontrôle rien : lancer `verify --dead-only` avant, pas à la place.
    """
    proprietaire = store is None
    store = store if store is not None else _store(args).load()
    supprimees = store.purge_dead()
    print(f"{supprimees} URLs mortes supprimées de l'index")
    if proprietaire:
        _report(store.write())
    return 0


def cmd_anomalies(args: argparse.Namespace, store: Store | None = None) -> int:
    """Inventorie les anomalies, et fait le ménage de celles qui ne mènent nulle part."""
    proprietaire = store is None
    store = store if store is not None else _store(args).load()
    par_type: dict[str, int] = {}
    for genre, _, _ in store.anomalies:
        par_type[genre] = par_type.get(genre, 0) + 1
    for genre, n in sorted(par_type.items(), key=lambda kv: -kv[1]):
        print(f"{n:>6}  {genre}")

    ecrire = False

    if args.drop_out_of_scope:
        # Pas besoin de recontrôler : « redirige hors périmètre » est un fait
        # structurel (mauvais hôte, mauvais format), pas un état qui flappe
        # comme la vivacité. On fait confiance au constat déjà posé par
        # `verify`/`dedupe` au moment de la détection.
        hors_perimetre = [t for t in store.anomalies if t[0] == "hors_perimetre"]
        retirees = 0
        for genre, url, detail in hors_perimetre:
            store.anomalies.discard((genre, url, detail))
            if store.remove(url):
                retirees += 1
        print(f"{retirees} URLs hors périmètre supprimées de l'index")
        ecrire = ecrire or retirees > 0

    if args.check or args.drop_dead:
        concernees = sorted({url for _, url, _ in store.anomalies})
        print(f"\ncontrôle de {len(concernees)} URLs...")
        verdicts = verify_module.check_urls(concernees)

        mortes = {
            url
            for url, (code, _) in verdicts.items()
            if code in config.VERIFY_DEAD_CODES
        }
        print(f"{len(mortes)} mortes, {len(concernees) - len(mortes)} encore vivantes ou non concluantes")

        if args.drop_dead:
            retirees_index = 0
            for genre, url, detail in list(store.anomalies):
                if url not in mortes:
                    continue
                store.anomalies.discard((genre, url, detail))
                # `trop_long` n'a jamais pu entrer dans l'index (c'est
                # précisément son motif) : il n'y a que la ligne de journal à
                # retirer.
                if store.remove(url):
                    retirees_index += 1
            print(f"{len(mortes)} anomalies retirées, dont {retirees_index} URLs ôtées de l'index")
            ecrire = True
        else:
            print("(--drop-dead pour les retirer de l'index et du journal d'anomalies)")
    elif not args.drop_out_of_scope:
        print(
            "\n(--check pour contrôler ces URLs, --drop-dead pour retirer celles qui sont "
            "mortes, --drop-out-of-scope pour retirer celles hors périmètre)"
        )

    if ecrire and proprietaire:
        _report(store.write())
    return 0


def cmd_build(args: argparse.Namespace, store: Store | None = None) -> int:
    """Relit puis réécrit ``/data`` : renormalise le tri et le sharding.

    ``force`` : c'est la seule commande qui réécrit même les dossiers
    inchangés. Un changement de seuil de sharding ou de projection des chemins
    ne salit aucun dossier — rien en mémoire n'a bougé — et resterait donc sans
    effet si ``build`` se contentait de l'écriture sélective.

    Écrit toujours immédiatement, même au sein d'un ``run`` : contrairement
    aux autres commandes, où écrire est un détail d'intendance, c'est ici
    tout l'intérêt de la commande. La déférer à la fin de la chaîne ferait
    perdre le ``force=True`` — un ``write()`` ordinaire plus tard ne
    trouverait de toute façon plus rien à faire, donc rien n'est gâché,
    mais rien n'est gagné non plus à attendre.
    """
    store = store if store is not None else _store(args).load()
    _report(store.write(force=True))
    return 0


def cmd_site(args: argparse.Namespace, store: Store | None = None) -> int:
    """Génère la page web de consultation de l'index."""
    store = store if store is not None else _store(args).load()
    if not store.dirs:
        print("Index vide. Lancer d'abord: python -m rts_indexer sitemap", file=sys.stderr)
        return 1
    path = explorer.generate(store, args.output)
    taille = path.stat().st_size / 1024
    print(f"{path} ({taille:.0f} Ko, {store.stats()['urls']} URLs)")
    return 0


def cmd_stats(args: argparse.Namespace, store: Store | None = None) -> int:
    store = store if store is not None else _store(args).load()
    _report(store.stats())
    return 0


def cmd_list(args: argparse.Namespace, store: Store | None = None) -> int:
    """Reconstruit les URLs complètes depuis ``/data`` (contrôle du mapping)."""
    store = store if store is not None else _store(args).load()
    for index, (url, dead) in enumerate(store.urls()):
        if args.limit and index >= args.limit:
            break
        print(f"{'!' if dead else ' '} {url}")
    return 0


def cmd_run(args: argparse.Namespace, store: Store | None = None) -> int:
    """Enchaîne plusieurs commandes sur un seul chargement et une seule écriture.

    Chaque commande est une ligne complète, avec ses propres options — le
    format qu'utilisent déjà les workflows GitHub Actions
    (``_collecte.yml``). Sans ça, un enchaînement de N commandes paie N
    cycles complets de lecture/écriture de l'index (le poste de coût
    dominant, mesuré à plusieurs minutes chacun) pour un travail qui n'en
    nécessite qu'un.

    Chaque commande reste responsable de sa propre résilience : une
    interruption ou une erreur au milieu d'une étape écrit immédiatement ce
    qui a été accumulé (voir ``cmd_crawl`` et consorts), plutôt que
    d'attendre une fin de chaîne qui n'arrivera pas.

    ``store`` n'est accepté que pour la cohérence de signature avec les
    autres commandes (``run`` peut apparaître comme ligne dans un fichier
    passé à un autre ``run``) ; un ``run`` imbriqué n'est pas pris en charge
    — relire l'entrée standard depuis l'intérieur d'une chaîne n'a pas de
    sens.
    """
    if store is not None:
        print("run ne peut pas être imbriqué dans un autre run", file=sys.stderr)
        return 1
    texte = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    lignes = [
        ligne.strip()
        for ligne in texte.splitlines()
        if ligne.strip() and not ligne.strip().startswith("#")
    ]
    if not lignes:
        print("Aucune commande à exécuter (fichier ou entrée standard vides).", file=sys.stderr)
        return 1

    store = _store(args).load()
    parser = build_parser()
    for ligne in lignes:
        print(f"--- {ligne} ---")
        try:
            sous_args = parser.parse_args(shlex.split(ligne))
        except SystemExit:
            print(f"commande invalide, ignorée : {ligne}", file=sys.stderr)
            continue
        sous_args.data_dir = args.data_dir  # un seul index pour toute la chaîne
        code = sous_args.func(sous_args, store=store)
        if code:
            print(f"« {ligne} » a échoué (code {code}), arrêt de la chaîne", file=sys.stderr)
            _report(store.write())
            return code

    _report(store.write())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rts_indexer",
        description="Indexeur des URLs de rts.ch — la base vit dans data/.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--data-dir",
        default=str(config.DATA_DIR),
        help="racine de l'index (défaut: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sitemap", help="collecte les sitemaps XML déclarés dans robots.txt")
    p.add_argument("--dry-run", action="store_true", help="n'écrit rien, affiche seulement")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_sitemap)

    p = sub.add_parser("rss", help="collecte les articles récents depuis les flux RSS")
    p.add_argument("--dry-run", action="store_true", help="n'écrit rien, affiche seulement")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_rss)

    p = sub.add_parser("crawl", help="parcourt les rubriques connues pour trouver les articles")
    p.add_argument(
        "--max-pages",
        type=int,
        default=500,
        help=(
            "budget de pages, plafond exact (défaut: %(default)s). "
            "0 = illimité : le crawl s'arrête de lui-même une fois toutes les "
            "rubriques connues visitées, la file d'attente n'étant pas infinie. "
            "Sert aussi de taille de tranche pour la rotation des graines : "
            "un run budgété ne repart jamais des mêmes rubriques que le précédent"
        ),
    )
    p.add_argument(
        "--include-articles",
        action="store_true",
        help="visite aussi les articles (coûteux, pour les liens connexes)",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="oublie la rotation des graines enregistrée, repart du début",
    )
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("wayback", help="collecte l'archive historique (Internet Archive)")
    p.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help=(
            "budget de pages CDX (défaut: %(default)s, 0 = illimité). "
            "Un parcours complet dépasse 1500 pages à ~10 s chacune : le "
            "curseur permet d'avancer par tranches successives"
        ),
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="oublie la progression enregistrée et repart de la page 0",
    )
    p.set_defaults(func=cmd_wayback)

    p = sub.add_parser("commoncrawl", help="collecte l'archive Common Crawl")
    p.add_argument(
        "--max-pages", type=int, default=50, help="budget de pages CDX (0 = illimité)"
    )
    p.add_argument(
        "--pages-per-index",
        type=int,
        default=5,
        help=(
            "budget par crawl (défaut: %(default)s). Deux crawls voisins se "
            "recouvrent beaucoup : mieux vaut en balayer plusieurs que creuser un seul"
        ),
    )
    p.add_argument(
        "--max-indexes",
        type=int,
        default=12,
        help="nombre de crawls considérés, du plus récent (défaut: %(default)s, 0 = tous)",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="oublie la progression enregistrée et repart du début",
    )
    p.set_defaults(func=cmd_commoncrawl)

    p = sub.add_parser("verify", help="contrôle quelles URLs répondent encore (sigil !)")
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="nombre maximal d'URLs à contrôler, les jamais-vues d'abord (0 = toutes)",
    )
    p.add_argument(
        "--recheck-days",
        type=int,
        default=config.VERIFY_RECHECK_DAYS,
        help="âge au-delà duquel une URL déjà contrôlée l'est à nouveau (défaut: %(default)s)",
    )
    p.add_argument(
        "--path",
        action="append",
        default=None,
        help=(
            "ne contrôler que les URLs sous ce préfixe "
            "(ex. www.rts.ch/meteo/ ou https://www.rts.ch/meteo/). "
            "Répétable pour cibler plusieurs sous-arbres à la fois."
        ),
    )
    p.add_argument(
        "--dead-only",
        action="store_true",
        help=(
            "ne recontrôler que les URLs déjà marquées mortes (sigil !), "
            "sans égard pour la fraîcheur du cache — un audit ponctuel"
        ),
    )
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser(
        "dedupe", help="supprime les URLs journalisées comme doublons par verify"
    )
    p.set_defaults(func=cmd_dedupe)

    p = sub.add_parser(
        "purge", help="supprime de l'index les URLs actuellement marquées mortes"
    )
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("import", help="ajoute les URLs listées dans un fichier texte")
    p.add_argument("fichier", help="fichier .txt, une URL par ligne (# = commentaire)")
    p.add_argument(
        "--check",
        action="store_true",
        help=(
            "contrôler chaque URL avant l'ajout : écarte les mortes, "
            "et indexe la cible plutôt que l'URL pour les redirections"
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="n'écrit rien, affiche seulement")
    p.add_argument("--limit", type=int, default=20, help="nombre de lignes détaillées affichées")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("anomalies", help="inventorie et nettoie le journal d'anomalies")
    p.add_argument("--check", action="store_true", help="contrôler les URLs concernées")
    p.add_argument(
        "--drop-dead",
        action="store_true",
        help="retirer les anomalies dont l'URL est morte (implique --check)",
    )
    p.add_argument(
        "--drop-out-of-scope",
        action="store_true",
        help="retirer de l'index les URLs hors_perimetre (redirigent hors périmètre, pas de recontrôle)",
    )
    p.set_defaults(func=cmd_anomalies)

    p = sub.add_parser("build", help="relit et réécrit data/ (tri, sharding, purge)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("site", help="génère la page web de consultation")
    p.add_argument(
        "--output",
        default=str(config.SITE_DIR),
        help="dossier de sortie (défaut: %(default)s)",
    )
    p.set_defaults(func=cmd_site)

    p = sub.add_parser("stats", help="compteurs de l'index")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("list", help="reconstruit les URLs complètes depuis data/")
    p.add_argument("--limit", type=int, default=0, help="0 = tout")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser(
        "run",
        help="enchaîne plusieurs commandes sur un seul chargement/écriture de l'index",
    )
    p.add_argument(
        "--file",
        default=None,
        help="fichier listant une commande complète par ligne (défaut : entrée standard)",
    )
    p.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    global _DEBUT
    args = build_parser().parse_args(argv)
    _log_setup(args.verbose)
    _DEBUT = time.monotonic()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Sans ce filet, l'interruption ressort de main() sans être rattrapée
        # nulle part et Python affiche sa propre trace brute par défaut, alors
        # que cmd_crawl a déjà écrit ce qu'il fallait et prévenu l'utilisateur.
        # 130 = convention Unix pour « terminé par Ctrl+C ».
        return 130
