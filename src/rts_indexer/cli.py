"""Interface en ligne de commande."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config
from .sources import crawl as crawl_source
from .sources import sitemap
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

    crawler = crawl_source.crawl(
        store,
        seeds,
        max_pages=args.max_pages,
        include_articles=args.include_articles,
    )
    print(
        f"{crawler.fetched} pages visitées "
        f"({crawler.from_cache} inchangées), {crawler.discovered} URLs nouvelles"
    )
    _report(store.write())
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Relit puis réécrit ``/data`` : renormalise le tri et le sharding."""
    _report(_store(args).load().write())
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

    p = sub.add_parser("crawl", help="parcourt les rubriques connues pour trouver les articles")
    p.add_argument("--max-pages", type=int, default=500, help="budget de pages (défaut: %(default)s)")
    p.add_argument(
        "--include-articles",
        action="store_true",
        help="visite aussi les articles (coûteux, pour les liens connexes)",
    )
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("build", help="relit et réécrit data/ (tri, sharding, purge)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("stats", help="compteurs de l'index")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("list", help="reconstruit les URLs complètes depuis data/")
    p.add_argument("--limit", type=int, default=0, help="0 = tout")
    p.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _log_setup(args.verbose)
    return args.func(args)
