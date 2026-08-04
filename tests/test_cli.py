"""Tests du CLI : surtout la résilience de `crawl` face à l'imprévu."""

import argparse

import pytest

from rts_indexer import cli
from rts_indexer.store import Store

ARTICLE = "https://www.rts.ch/info/suisse/2026/article/x-1.html"


def _args(tmp_path, **overrides):
    ns = argparse.Namespace(
        data_dir=str(tmp_path),
        max_pages=10,
        include_articles=False,
        reset=False,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


@pytest.mark.parametrize("erreur", [RuntimeError("boum"), KeyboardInterrupt()])
def test_crawl_ecrit_les_progres_avant_de_propager(tmp_path, monkeypatch, erreur):
    """Incident réel : un crawl qui plante en cours de route perdait tout son
    travail, faute d'écrire le store avant que l'erreur ne remonte. Une erreur
    imprévue *ou* un Ctrl+C doivent tous deux préserver ce qui a été trouvé."""
    # `select_seeds` écrit son curseur de rotation dans config.CACHE_DIR : sans
    # cet isolement, ce test toucherait le vrai .cache/ du dépôt.
    monkeypatch.setattr(cli.config, "CACHE_DIR", tmp_path / "cache")
    store = Store(tmp_path)
    store.add("https://www.rts.ch/info/suisse/")
    store.write()

    def crawl_qui_plante(store, seeds, **kwargs):
        store.add(ARTICLE)  # du travail a été accompli avant l'incident
        raise erreur

    monkeypatch.setattr(cli.crawl_source, "crawl", crawl_qui_plante)

    with pytest.raises(type(erreur)):
        cli.cmd_crawl(_args(tmp_path))

    relu = Store(tmp_path).load()
    assert dict(relu.urls())[ARTICLE] is False


def test_main_absorbe_ctrl_c_avec_le_code_de_sortie_conventionnel(tmp_path, monkeypatch, capsys):
    """`main()` ne doit pas laisser Python afficher sa propre trace brute pour
    une interruption volontaire : `cmd_crawl` a déjà écrit le nécessaire et
    prévenu l'utilisateur, il ne reste qu'à sortir proprement (code 130, la
    convention Unix pour « terminé par Ctrl+C »)."""
    monkeypatch.setattr(cli.config, "CACHE_DIR", tmp_path / "cache")
    store = Store(tmp_path)
    store.add("https://www.rts.ch/info/suisse/")
    store.write()

    def crawl_interrompu(store, seeds, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.crawl_source, "crawl", crawl_interrompu)

    code = cli.main(["--data-dir", str(tmp_path), "crawl"])

    assert code == 130
    assert "Traceback" not in capsys.readouterr().err
