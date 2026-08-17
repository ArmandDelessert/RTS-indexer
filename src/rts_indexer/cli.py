"""Interface en ligne de commande."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import config, explorer
from . import verify as verify_module
from .sources import commoncrawl, rss, sitemap, wayback
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


def cmd_sitemap(args: argparse.Namespace) -> int:
    urls = sitemap.collect()
    print(f"{len(urls)} URLs canoniques collectées depuis les sitemaps")
    if args.dry_run:
        for url in urls[: args.limit]:
            print(f"  {url}")
        return 0
    store = _store(args).load()
    added = store.add_many(urls)
    print(f"{added} nouvelles")
    _report(store.write())
    return 0


def cmd_rss(args: argparse.Namespace) -> int:
    """Collecte les articles fraîchement publiés depuis les flux RSS."""
    urls = rss.collect()
    print(f"{len(urls)} URLs collectées depuis {len(rss.feed_urls())} flux RSS")
    if args.dry_run:
        for url in urls[: args.limit]:
            print(f"  {url}")
        return 0
    store = _store(args).load()
    added = store.add_many(urls)
    print(f"{added} nouvelles")
    _report(store.write())
    return 0


def cmd_crawl(args: argparse.Namespace) -> int:
    """Parcourt les rubriques déjà connues pour en extraire les articles."""
    store = _store(args).load()
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
        # trace complète, juste la confirmation que rien n'est perdu.
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
    _report(store.write())
    return 0


def cmd_wayback(args: argparse.Namespace) -> int:
    """Collecte l'archive historique via l'API CDX d'Internet Archive."""
    store = _store(args).load()
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
    _report(store.write())
    return 0


def cmd_commoncrawl(args: argparse.Namespace) -> int:
    """Collecte l'archive de la fondation Common Crawl."""
    store = _store(args).load()
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
    _report(store.write())
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Contrôle la vivacité des URLs indexées et met à jour le sigil `!`."""
    store = _store(args).load()
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
    _report(store.write())
    return 0


def cmd_dedupe(args: argparse.Namespace) -> int:
    """Supprime les URLs journalisées comme doublons par `verify`."""
    store = _store(args).load()
    supprimes, ajoutees, ignores = store.resolve_doublons()
    print(
        f"{supprimes} doublons supprimés ({ajoutees} cibles nouvellement indexées), "
        f"{ignores} requalifiés hors périmètre (pas de vrai doublon)"
    )
    _report(store.write())
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    """Supprime de l'index les URLs actuellement marquées mortes.

    Par défaut, une URL morte reste indexée (sigil `!`) — cette commande
    n'est là que pour qui préfère explicitement un index de contenu vivant.
    Ne recontrôle rien : lancer `verify --dead-only` avant, pas à la place.
    """
    store = _store(args).load()
    supprimees = store.purge_dead()
    print(f"{supprimees} URLs mortes supprimées de l'index")
    _report(store.write())
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Relit puis réécrit ``/data`` : renormalise le tri et le sharding.

    ``force`` : c'est la seule commande qui réécrit même les dossiers
    inchangés. Un changement de seuil de sharding ou de projection des chemins
    ne salit aucun dossier — rien en mémoire n'a bougé — et resterait donc sans
    effet si ``build`` se contentait de l'écriture sélective.
    """
    _report(_store(args).load().write(force=True))
    return 0


def cmd_site(args: argparse.Namespace) -> int:
    """Génère la page web de consultation de l'index."""
    store = _store(args).load()
    if not store.dirs:
        print("Index vide. Lancer d'abord: python -m rts_indexer sitemap", file=sys.stderr)
        return 1
    path = explorer.generate(store, args.output)
    taille = path.stat().st_size / 1024
    print(f"{path} ({taille:.0f} Ko, {store.stats()['urls']} URLs)")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    _report(_store(args).load().stats())
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Reconstruit les URLs complètes depuis ``/data`` (contrôle du mapping)."""
    for index, (url, dead) in enumerate(_store(args).load().urls()):
        if args.limit and index >= args.limit:
            break
        print(f"{'!' if dead else ' '} {url}")
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
