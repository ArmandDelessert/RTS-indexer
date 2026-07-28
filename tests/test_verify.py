"""Tests du contrôle de vivacité.

Le comportement décisif est la prudence du verdict : seuls 404/410 marquent une
URL morte. Un 403, un 429, un 5xx ou une coupure réseau sont non concluants —
sans quoi une limitation de débit passagère enterrerait des milliers d'URLs
vivantes d'un coup.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from rts_indexer import config
from rts_indexer.store import Store
from rts_indexer.verify import Verifier

VIVANTE = "https://www.rts.ch/info/suisse/2026/article/vivante-1.html"
MORTE = "https://www.rts.ch/info/suisse/2026/article/morte-2.html"
RUBRIQUE = "https://www.rts.ch/info/suisse/"

#: Chemin -> code renvoyé par le faux serveur.
CODES = {
    "/info/suisse/2026/article/vivante-1.html": 200,
    "/info/suisse/2026/article/morte-2.html": 404,
    "/info/suisse/": 200,
}


@pytest.fixture(autouse=True)
def _sans_attente(monkeypatch):
    monkeypatch.setattr(config, "VERIFY_MIN_INTERVAL", 0.0)


def _transport(codes=None, log=None):
    codes = CODES if codes is None else codes

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append((request.method, request.url.path))
        return httpx.Response(codes.get(request.url.path, 404))

    return httpx.MockTransport(handler)


def _store(tmp_path, urls=(VIVANTE, MORTE, RUBRIQUE)):
    store = Store(tmp_path / "data")
    store.add_many(urls)
    return store


def _verifier(tmp_path, store=None, **kwargs):
    kwargs.setdefault("transport", _transport(kwargs.pop("codes", None), kwargs.pop("log", None)))
    kwargs.setdefault("cache_dir", tmp_path / "cache")
    return Verifier(store or _store(tmp_path), **kwargs)


# -- verdict -----------------------------------------------------------------


def test_404_marque_l_url_morte(tmp_path):
    verifier = _verifier(tmp_path)
    asyncio.run(verifier.run())

    statuts = dict(verifier.store.urls())
    assert statuts[MORTE] is True
    assert statuts[VIVANTE] is False
    assert verifier.morts == 1


def test_rubrique_morte_est_marquee(tmp_path):
    """Cas réel : dossiers/2016/coeur-a-coeur/* sont des *rubriques* en 404.
    C'est précisément le cas que `add()` ignorait silencieusement."""
    codes = {"/info/suisse/": 404}
    store = _store(tmp_path, urls=[RUBRIQUE])
    verifier = _verifier(tmp_path, store=store, codes=codes)
    asyncio.run(verifier.run())

    assert verifier.store.status(RUBRIQUE) is True
    assert verifier.morts == 1


def test_url_ressuscitee_perd_son_sigil(tmp_path):
    """Une URL rétablie côté site doit retrouver son statut vivant."""
    store = _store(tmp_path)
    store.add(VIVANTE, dead=True)  # marquée morte lors d'un run précédent

    verifier = _verifier(tmp_path, store=store)
    asyncio.run(verifier.run())

    assert dict(verifier.store.urls())[VIVANTE] is False
    assert verifier.ressuscites == 1


@pytest.mark.parametrize("code", [403, 429, 500, 503])
def test_codes_non_concluants_ne_marquent_rien(tmp_path, code):
    """Un 403 (rts.ch en renvoie sur des pages bien vivantes), une limitation
    de débit ou une panne serveur ne sont pas des preuves de mort."""
    codes = {"/info/suisse/2026/article/vivante-1.html": code}
    store = _store(tmp_path, urls=[VIVANTE])
    verifier = _verifier(tmp_path, store=store, codes=codes)
    asyncio.run(verifier.run())

    assert dict(verifier.store.urls())[VIVANTE] is False  # inchangé
    assert verifier.morts == 0
    assert verifier.non_concluants == 1
    # Rien en cache : l'URL sera recontrôlée au prochain run.
    assert VIVANTE not in verifier.cache


def test_erreur_reseau_non_concluante(tmp_path):
    def handler(request):
        raise httpx.ConnectError("réseau coupé")

    store = _store(tmp_path, urls=[VIVANTE])
    verifier = _verifier(tmp_path, store=store, transport=httpx.MockTransport(handler))
    asyncio.run(verifier.run())

    assert verifier.non_concluants == 1
    assert dict(verifier.store.urls())[VIVANTE] is False
    assert verifier.cache == {}


def test_head_puis_get_si_la_methode_est_refusee(tmp_path):
    """Certains CDN n'implémentent pas HEAD : le GET de repli doit trancher."""
    appels: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append((request.method, request.url.path))
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(404)

    store = _store(tmp_path, urls=[MORTE])
    verifier = _verifier(tmp_path, store=store, transport=httpx.MockTransport(handler))
    asyncio.run(verifier.run())

    assert [methode for methode, _ in appels] == ["HEAD", "GET"]
    assert dict(verifier.store.urls())[MORTE] is True


# -- sélection ---------------------------------------------------------------


def test_le_cache_evite_de_recontroler(tmp_path):
    cache_dir = tmp_path / "cache"
    premier = _verifier(tmp_path, cache_dir=cache_dir)
    asyncio.run(premier.run())
    assert premier.checked == 3

    second = _verifier(tmp_path, cache_dir=cache_dir)
    asyncio.run(second.run())
    assert second.checked == 0  # tout est frais, rien à refaire


def test_une_entree_perimee_est_recontrolee(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    vieux = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
    (cache_dir / "verify.json").write_text(
        json.dumps({VIVANTE: {"checked_at": vieux, "status": 200}}), encoding="utf-8"
    )

    verifier = _verifier(tmp_path, cache_dir=cache_dir, recheck_days=30)
    asyncio.run(verifier.run())
    assert verifier.checked == 3  # la périmée + les deux jamais vues


def test_limite_priorise_les_urls_jamais_vues(tmp_path):
    """Avec un budget, on avance sur le front inconnu plutôt que de
    re-contrôler indéfiniment les mêmes anciennes."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    vieux = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
    (cache_dir / "verify.json").write_text(
        json.dumps({RUBRIQUE: {"checked_at": vieux, "status": 200}}), encoding="utf-8"
    )

    verifier = _verifier(tmp_path, cache_dir=cache_dir, max_urls=2)
    verifier.load_cache()
    # RUBRIQUE est périmée mais déjà connue : elle passe après les inconnues,
    # et le budget de 2 est donc consommé par ces dernières.
    assert set(verifier.pending()) == {VIVANTE, MORTE}


def test_cache_corrompu_repart_a_vide(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "verify.json").write_bytes(b"{pas du JSON")

    verifier = _verifier(tmp_path, cache_dir=cache_dir)
    asyncio.run(verifier.run())
    assert verifier.checked == 3


# -- robustesse --------------------------------------------------------------


def test_une_url_qui_plante_n_interrompt_pas_le_run(tmp_path, monkeypatch):
    """Comme pour le crawl : un run qui peut durer des heures ne doit jamais
    être arrêté par une seule URL."""
    verifier = _verifier(tmp_path)
    original = verifier.store.add

    def add_qui_plante(url, **kwargs):
        if url == VIVANTE:
            raise RuntimeError("boum")
        return original(url, **kwargs)

    monkeypatch.setattr(verifier.store, "add", add_qui_plante)
    asyncio.run(verifier.run())

    assert verifier.checked == 3  # les trois ont bien été tentées
    assert dict(verifier.store.urls())[MORTE] is True  # les autres ont abouti


def test_index_vide_ne_fait_rien(tmp_path):
    verifier = _verifier(tmp_path, store=Store(tmp_path / "data"))
    asyncio.run(verifier.run())
    assert verifier.checked == 0
