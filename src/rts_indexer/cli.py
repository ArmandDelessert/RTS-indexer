"""Interface en ligne de commande."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config, explorer
from . import verify as verify_module
from .sources import commoncrawl
from .sources import crawl as crawl_source
from .sources import rss, sitemap, wayback
from .store import Store


def _store(args: argparse.Namespace) -> Store:
    return Store(Path(args.data_dir))


def _log_setup(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )


def _report(stats: dict[str, int]) -> None:
    width = max(len(k) for k in stats)
    for key, value in stats.items():
        print(f"{key.replace('_', ' '):<{width}}  {value:>9,}".replace(",", "'"))


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
    seeds = [url for url, _ in store.urls() if url.endswith("/")]
    if not seeds:
        print(
            "Aucune rubrique dans l'index. Lancer d'abord: python -m rts_indexer sitemap",
            file=sys.stderr,
        )
        return 1
    print(f"{len(seeds)} rubriques en graine, budget {args.max_pages} pages")

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
        logging.exception("le crawl s'est interrompu, écriture des URLs déjà découvertes")
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
        logging.exception("la collecte s'est interrompue, écriture des URLs obtenues")
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
        logging.exception("la collecte s'est interrompue, écriture des URLs obtenues")
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
        )
    except KeyboardInterrupt:
        print("\nInterruption : écriture des verdicts déjà obtenus...", file=sys.stderr)
        _report(store.write())
        raise
    except Exception:
        logging.exception("le contrôle s'est interrompu, écriture des verdicts obtenus")
        _report(store.write())
        raise

    print(
        f"{verifier.checked} URLs contrôlées : "
        f"{verifier.morts} nouvellement mortes, "
        f"{verifier.ressuscites} de nouveau vivantes, "
        f"{verifier.non_concluants} non concluantes"
    )
    _report(store.write())
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Relit puis réécrit ``/data`` : renormalise le tri et le sharding."""
    _report(_store(args).load().write())
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
            "rubriques connues visitées, la file d'attente n'étant pas infinie"
        ),
    )
    p.add_argument(
        "--include-articles",
        action="store_true",
        help="visite aussi les articles (coûteux, pour les liens connexes)",
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
    p.set_defaults(func=cmd_verify)

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
    args = build_parser().parse_args(argv)
    _log_setup(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Sans ce filet, l'interruption ressort de main() sans être rattrapée
        # nulle part et Python affiche sa propre trace brute par défaut, alors
        # que cmd_crawl a déjà écrit ce qu'il fallait et prévenu l'utilisateur.
        # 130 = convention Unix pour « terminé par Ctrl+C ».
        return 130
