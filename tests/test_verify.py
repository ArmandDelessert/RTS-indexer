"""Tests du contrôle de vivacité.

Le comportement décisif est la prudence du verdict : seuls 404/410 marquent une
URL morte. Un 403, un 429, un 5xx ou une coupure réseau sont non concluants —
sans quoi une limitation de débit passagère enterrerait des milliers d'URLs
vivantes d'un coup.
"""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

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
    # Sans ça, le moindre 404 ferait attendre le délai réel (60 s) avant le
    # second avis, et chaque test durerait une minute.
    kwargs.setdefault("retry_delay", 0)
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

    # Deux tentatives : le 404 vaut un second avis (cf. VERIFY_RETRY_CODES), et
    # chacune rejoue le repli HEAD -> GET.
    assert [methode for methode, _ in appels] == ["HEAD", "GET", "HEAD", "GET"]
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
    vieux = (datetime.now(UTC) - timedelta(days=90)).isoformat(timespec="seconds")
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
    vieux = (datetime.now(UTC) - timedelta(days=90)).isoformat(timespec="seconds")
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


# -- second avis avant de condamner ------------------------------------------


def test_404_isole_ne_condamne_pas_du_premier_coup(tmp_path):
    """Le cas qui motive tout : un 404 passager (incident serveur ou cache
    négatif de CDN) ne doit pas suffire à enterrer une URL vivante."""
    appels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request.method)
        # 404 au premier passage, 200 au second : le sursis doit la sauver.
        return httpx.Response(404 if len(appels) == 1 else 200)

    store = _store(tmp_path, urls=[MORTE])
    verifier = _verifier(tmp_path, store=store, transport=httpx.MockTransport(handler))
    asyncio.run(verifier.run())

    assert verifier.reessais == 1
    assert verifier.morts == 0
    assert dict(verifier.store.urls())[MORTE] is False


def test_404_confirme_condamne(tmp_path):
    verifier = _verifier(tmp_path, store=_store(tmp_path, urls=[MORTE]))
    asyncio.run(verifier.run())

    assert verifier.reessais == 1
    assert verifier.morts == 1
    assert dict(verifier.store.urls())[MORTE] is True
    # Une seule URL contrôlée, malgré les deux requêtes.
    assert verifier.checked == 1


def test_410_ne_vaut_pas_de_second_avis(tmp_path):
    """410 = suppression explicite et délibérée du serveur. Mesuré : 70 % des
    URLs mortes répondent 410, les réinterroger serait du gaspillage pur."""
    appels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request.method)
        return httpx.Response(410)

    store = _store(tmp_path, urls=[MORTE])
    verifier = _verifier(tmp_path, store=store, transport=httpx.MockTransport(handler))
    asyncio.run(verifier.run())

    assert len(appels) == 1
    assert verifier.reessais == 0
    assert dict(verifier.store.urls())[MORTE] is True


def test_non_concluant_est_reessaye_dans_le_meme_run(tmp_path):
    """Un 429 ou un 5xx est transitoire par nature : mieux vaut réessayer
    quelques instants plus tard qu'attendre le prochain run, des semaines
    après."""
    appels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request.method)
        return httpx.Response(503 if len(appels) == 1 else 200)

    store = _store(tmp_path, urls=[VIVANTE])
    verifier = _verifier(tmp_path, store=store, transport=httpx.MockTransport(handler))
    asyncio.run(verifier.run())

    assert verifier.reessais == 1
    assert verifier.non_concluants == 0
    assert dict(verifier.store.urls())[VIVANTE] is False


def test_non_concluant_persistant_reste_non_concluant(tmp_path):
    """Épuiser les essais ne transforme pas un doute en verdict : ni sigil, ni
    cache, on retentera au prochain run."""
    verifier = _verifier(
        tmp_path,
        store=_store(tmp_path, urls=[VIVANTE]),
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    asyncio.run(verifier.run())

    assert verifier.non_concluants == 1
    assert verifier.cache == {}  # rien mis en cache : à recontrôler
    assert dict(verifier.store.urls())[VIVANTE] is False  # sigil intact


def test_le_sursis_pose_une_echeance_dans_le_futur(tmp_path):
    """Le second avis n'est pas immédiat : l'URL passe par une file à échéance,
    sans quoi on retomberait sur la même réponse en cache côté CDN."""
    verifier = _verifier(tmp_path, retry_delay=30.0)

    assert verifier._reprogrammer(MORTE, "HTTP 404") is True
    echeance, url = verifier._differes[0]
    assert url == MORTE
    assert echeance > time.monotonic() + 25  # ~30 s dans le futur
    # L'échéance n'étant pas atteinte, rien n'est encore à reprendre.
    assert verifier._differe_du() is None


def test_les_essais_sont_bornes(tmp_path):
    """Sans borne, une URL durablement en erreur boucherait la file
    indéfiniment."""
    verifier = _verifier(tmp_path, retry_delay=0)

    assert verifier._reprogrammer(MORTE, "HTTP 404") is True
    assert verifier._reprogrammer(MORTE, "HTTP 404") is False
    assert verifier.reessais == 1


# -- doublons (redirections) -------------------------------------------------


def test_redirection_signale_un_doublon(tmp_path):
    """Une variante de slug répond 200 en redirigeant vers l'article canonique :
    elle paraissait saine, le suivi de l'URL finale la démasque."""
    canonique = "https://www.rts.ch/info/suisse/2026/article/vivante-1.html"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("variante-9.html"):
            return httpx.Response(301, headers={"Location": canonique})
        return httpx.Response(200)

    variante = "https://www.rts.ch/info/suisse/2026/article/variante-9.html"
    store = _store(tmp_path, urls=[variante])
    verifier = _verifier(tmp_path, store=store, transport=httpx.MockTransport(handler))
    asyncio.run(verifier.run())

    assert verifier.redirections == {variante: canonique}
    assert ("doublon", variante, canonique) in store.anomalies
    # Journalisé seulement : rien n'est supprimé à ce stade.
    assert dict(store.urls())[variante] is False


def test_une_url_sans_redirection_n_est_pas_un_doublon(tmp_path):
    verifier = _verifier(tmp_path, store=_store(tmp_path, urls=[VIVANTE]))
    asyncio.run(verifier.run())
    assert verifier.redirections == {}


def test_anomalie_doublon_survit_au_rechargement(tmp_path):
    """Contrairement aux autres anomalies, un doublon ne peut pas être rejoué
    hors ligne : il vient d'une redirection constatée sur le réseau."""
    store = Store(tmp_path / "data")
    store.add(VIVANTE)
    store.anomalies.add(("doublon", VIVANTE, "https://www.rts.ch/canonique.html"))
    store.write()

    relu = Store(tmp_path / "data").load()
    assert ("doublon", VIVANTE, "https://www.rts.ch/canonique.html") in relu.anomalies


# -- ciblage par sous-arbre --------------------------------------------------


def test_path_restreint_le_controle_a_un_sous_arbre(tmp_path):
    meteo = "https://www.rts.ch/meteo/previsions-1.html"
    store = _store(tmp_path, urls=[VIVANTE, MORTE, meteo])
    verifier = _verifier(tmp_path, store=store, path_prefix="www.rts.ch/meteo/")
    assert verifier.pending() == [meteo]


def test_path_accepte_l_url_complete(tmp_path):
    meteo = "https://www.rts.ch/meteo/previsions-1.html"
    store = _store(tmp_path, urls=[VIVANTE, meteo])
    verifier = _verifier(
        tmp_path, store=store, path_prefix="https://www.rts.ch/meteo/"
    )
    assert verifier.pending() == [meteo]


def test_sans_path_tout_est_controle(tmp_path):
    verifier = _verifier(tmp_path)
    assert len(verifier.pending()) == 3


def test_path_avec_antislash_est_normalise(tmp_path):
    """Incident réel : `data\\www.rts.ch\\meteo` copié depuis l'explorateur
    Windows contient des antislashs, alors que les URLs n'en portent jamais.
    Sans normalisation, --path ne matche jamais rien, silencieusement."""
    meteo = "https://www.rts.ch/meteo/previsions-1.html"
    store = _store(tmp_path, urls=[VIVANTE, meteo])
    verifier = _verifier(tmp_path, store=store, path_prefix="www.rts.ch\\meteo\\")
    assert verifier.pending() == [meteo]


def test_path_sans_slash_final_ne_matche_pas_un_dossier_voisin(tmp_path):
    """Incident réel : --path www.rts.ch/a (sans slash) a matché audio-podcast,
    archives, audio... 226'344 URLs au lieu des ~2'000 visées, parce que
    startswith() compare du texte et non des segments de chemin."""
    voisin = "https://www.rts.ch/audio-podcast/2018/audio/x.html"
    cible = "https://www.rts.ch/a/y.html"
    store = _store(tmp_path, urls=[voisin, cible])
    verifier = _verifier(tmp_path, store=store, path_prefix="www.rts.ch/a")
    assert verifier.pending() == [cible]


def test_path_sans_slash_final_avertit(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        _verifier(tmp_path, path_prefix="www.rts.ch/a")
    assert any("complété" in m for m in caplog.messages)


def test_path_vers_une_url_precise_n_est_pas_altere(tmp_path):
    """Un --path qui vise une URL précise (segment terminal avec un point,
    comme un .html) ne doit pas recevoir de slash : ça la rendrait
    méconnaissable, et donc introuvable."""
    precise = "https://www.rts.ch/meteo/previsions-1.html"
    store = _store(tmp_path, urls=[precise])
    verifier = _verifier(tmp_path, store=store, path_prefix=precise)
    assert verifier.path_prefixes == (precise,)  # pas de slash ajouté
    assert verifier.pending() == [precise]


def test_path_avec_slash_final_deja_present_est_inchange(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        verifier = _verifier(tmp_path, path_prefix="www.rts.ch/meteo/")
    assert verifier.path_prefixes == ("https://www.rts.ch/meteo/",)
    assert not any("complété" in m for m in caplog.messages)


def test_plusieurs_path_ciblent_plusieurs_sous_arbres(tmp_path):
    meteo = "https://www.rts.ch/meteo/previsions-1.html"
    sport = "https://www.rts.ch/sport/football/match-1.html"
    store = _store(tmp_path, urls=[VIVANTE, meteo, sport])
    verifier = _verifier(
        tmp_path,
        store=store,
        path_prefix=["www.rts.ch/meteo/", "www.rts.ch/sport/"],
    )
    assert sorted(verifier.pending()) == sorted([meteo, sport])


def test_path_qui_ne_matche_rien_previent_explicitement(tmp_path, caplog):
    """Un --path fautif ne doit pas se confondre avec un index déjà à jour :
    c'est exactement l'ambiguïté qui a fait perdre un run entier."""
    verifier = _verifier(tmp_path, path_prefix="www.rts.ch/inexistant/")
    with caplog.at_level("WARNING"):
        asyncio.run(verifier.run())

    assert verifier.checked == 0
    assert any("aucune URL" in m for m in caplog.messages)


def test_path_qui_matche_mais_deja_a_jour_reste_discret(tmp_path, caplog):
    """À l'inverse, un --path valide dont tout est déjà frais ne doit pas
    déclencher le même avertissement : ce n'est pas une erreur."""
    verifier = _verifier(tmp_path, path_prefix="www.rts.ch/info/")
    asyncio.run(verifier.run())  # premier passage : tout devient "vu"

    with caplog.at_level("WARNING"):
        asyncio.run(verifier.run())  # second passage : rien de neuf, normal

    assert not any("aucune URL" in m for m in caplog.messages)


# -- progression --------------------------------------------------------------


def test_progression_rappelle_le_total_et_le_pourcentage(tmp_path):
    verifier = _verifier(tmp_path)
    verifier._total = 20
    verifier._debut = time.monotonic() - 10
    verifier.checked = 5

    assert verifier._progression() == "5/20 (25%), 10 s écoulées, ~30 s restantes"


def test_progression_sans_estimation_avant_le_premier_verdict(tmp_path):
    """Sans aucune donnée de débit, une estimation serait inventée : mieux
    vaut l'omettre que d'afficher un chiffre sans fondement."""
    verifier = _verifier(tmp_path)
    verifier._total = 20
    verifier._debut = time.monotonic()
    verifier.checked = 0

    assert "restantes" not in verifier._progression()


def test_progression_ne_depasse_pas_100_pour_cent_a_la_fin(tmp_path):
    verifier = _verifier(tmp_path)
    verifier._total = 20
    verifier._debut = time.monotonic() - 60
    verifier.checked = 20

    resultat = verifier._progression()
    assert "100%" in resultat
    assert "restantes" not in resultat  # plus rien à estimer, le run est fini


def test_le_checkpoint_inclut_la_progression(tmp_path, caplog):
    """Le rapport initial (`checkpoint: 5500 URLs contrôlées`) ne rappelait
    pas le total : impossible de juger l'avancement d'un coup d'œil."""
    verifier = _verifier(tmp_path, store=_store(tmp_path, urls=[VIVANTE]))
    verifier._total = 1
    verifier._debut = time.monotonic()
    verifier.checked = 1

    with caplog.at_level("INFO"):
        verifier._checkpoint()

    assert any("1/1 (100%)" in m for m in caplog.messages)


def test_pas_de_checkpoint_redondant_apres_une_reprogrammation(tmp_path, monkeypatch):
    """Une URL reprogrammée (second avis) ne fait pas avancer `checked` : le
    garde de _worker doit l'empêcher de redéclencher le même checkpoint."""
    monkeypatch.setattr(config, "VERIFY_CHECKPOINT_URLS", 1)
    appels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request.method)
        # 404 puis 200 : une reprogrammation avant le verdict final.
        return httpx.Response(404 if len(appels) == 1 else 200)

    store = _store(tmp_path, urls=[MORTE])
    verifier = _verifier(tmp_path, store=store, transport=httpx.MockTransport(handler))

    checkpoints: list[int] = []
    original = verifier._checkpoint

    def checkpoint_compte():
        checkpoints.append(verifier.checked)
        original()

    monkeypatch.setattr(verifier, "_checkpoint", checkpoint_compte)
    asyncio.run(verifier.run())

    # Un seul checkpoint, une fois le verdict réellement obtenu — pas un par
    # passage de worker.
    assert checkpoints == [1]


# -- dead_only -----------------------------------------------------------


def test_dead_only_ne_selectionne_que_les_urls_mortes(tmp_path):
    store = _store(tmp_path)
    store.add(MORTE, dead=True)
    verifier = _verifier(tmp_path, store=store, dead_only=True)

    assert verifier.pending() == [MORTE]


def test_dead_only_ignore_la_fraicheur_du_cache(tmp_path):
    """Le point : contrairement au flux incrémental habituel, une URL déjà
    contrôlée récemment doit quand même ressortir — c'est un audit demandé
    explicitement, pas une reprise du front inconnu."""
    store = _store(tmp_path)
    store.add(MORTE, dead=True)
    verifier = _verifier(tmp_path, store=store, dead_only=True)
    verifier.cache[MORTE] = {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": 404,
    }

    assert verifier.pending() == [MORTE]


def test_dead_only_respecte_path_et_limit(tmp_path):
    autre_morte = "https://www.rts.ch/meteo/vieille-1.html"
    store = _store(tmp_path)
    store.add(MORTE, dead=True)
    store.add(autre_morte, dead=True)

    verifier = _verifier(tmp_path, store=store, dead_only=True, path_prefix="www.rts.ch/meteo/")
    assert verifier.pending() == [autre_morte]


def test_dead_only_peut_ressusciter_une_url(tmp_path):
    """Le cas d'usage réel : une URL morte qui répond de nouveau doit perdre
    son sigil, exactement comme le flux normal — dead_only ne change que la
    sélection, pas le verdict."""
    store = _store(tmp_path, urls=[RUBRIQUE])
    store.add(MORTE, dead=True)
    codes = {"/info/suisse/2026/article/morte-2.html": 200}
    verifier = _verifier(tmp_path, store=store, dead_only=True, codes=codes)

    asyncio.run(verifier.run())

    assert verifier.ressuscites == 1
    assert dict(verifier.store.urls())[MORTE] is False


def test_dead_only_vide_ne_fait_rien(tmp_path):
    verifier = _verifier(tmp_path, dead_only=True)  # aucune URL morte
    asyncio.run(verifier.run())
    assert verifier.checked == 0
