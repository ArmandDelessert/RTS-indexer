"""Tests du stockage : format, déterminisme, sharding, détection de collision."""

import json

import pytest

from rts_indexer import config
from rts_indexer.store import Store

RUBRIQUE = "https://www.rts.ch/info/suisse/"
ARTICLE = "https://www.rts.ch/info/suisse/2026/article/la-suisse-29312521.html"
AUTRE = "https://www.rts.ch/info/suisse/2026/article/paleo-festival-29313279.html"
DOSSIER = "www.rts.ch/info/suisse/2026/article"


def index_de(data_dir, relpath=DOSSIER, infix=""):
    path = data_dir / relpath / f"{config.INDEX_BASENAME}{infix}{config.INDEX_SUFFIX}"
    return path.read_text(encoding="utf-8")


def test_format_du_fichier(tmp_path):
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add(AUTRE)
    store.add(RUBRIQUE)
    store.write()

    # Rubrique : le dossier est lui-même une URL -> ligne `./`.
    assert index_de(tmp_path, "www.rts.ch/info/suisse") == "./\n"
    # Articles : triés, un slug par ligne, sans préfixe.
    assert index_de(tmp_path) == (
        "la-suisse-29312521.html\npaleo-festival-29313279.html\n"
    )


def test_lf_force_meme_sous_windows(tmp_path):
    """Un CRLF ferait diverger le dépôt à chaque run sur des milliers de lignes."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.write()
    brut = (tmp_path / DOSSIER / "_index.txt").read_bytes()
    assert b"\r" not in brut


def test_aller_retour_complet(tmp_path):
    urls = [RUBRIQUE, ARTICLE, AUTRE, "https://www.rts.ch/"]
    store = Store(tmp_path)
    store.add_many(urls)
    store.write()

    relu = Store(tmp_path).load()
    assert sorted(url for url, _ in relu.urls()) == sorted(urls)


def test_determinisme_du_second_run(tmp_path):
    """Réécrire sans nouveauté amont doit produire des octets identiques."""
    store = Store(tmp_path)
    store.add_many([RUBRIQUE, ARTICLE, AUTRE])
    store.write()
    avant = index_de(tmp_path)

    Store(tmp_path).load().write()
    assert index_de(tmp_path) == avant


def test_sigil_mort_ne_change_qu_une_ligne(tmp_path):
    """Le tri ignore le sigil : marquer une URL morte doit modifier la ligne sur
    place, pas la déplacer dans le fichier."""
    store = Store(tmp_path)
    store.add_many([ARTICLE, AUTRE])
    store.write()
    avant = index_de(tmp_path).splitlines()

    store = Store(tmp_path).load()
    store.add(ARTICLE, dead=True)
    store.write()
    apres = index_de(tmp_path).splitlines()

    assert len(avant) == len(apres)
    differences = [i for i, (a, b) in enumerate(zip(avant, apres)) if a != b]
    assert differences == [0]
    assert apres[0] == f"{config.DEAD_SIGIL}la-suisse-29312521.html"

    # Et le statut survit à une relecture.
    assert dict(Store(tmp_path).load().urls())[ARTICLE] is True


def test_source_ne_ressuscite_pas_une_url_morte(tmp_path):
    """Une source qui réaffirme une URL ne doit pas écraser le verdict de verify."""
    store = Store(tmp_path)
    store.add(ARTICLE, dead=True)
    store.write()

    store = Store(tmp_path).load()
    store.add(ARTICLE)  # dead=None : statut inchangé
    store.write()
    assert dict(Store(tmp_path).load().urls())[ARTICLE] is True


def test_sharding_au_dela_du_seuil(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SHARD_THRESHOLD", 3)
    store = Store(tmp_path)
    store.add(RUBRIQUE)
    for lettre in "abcd":
        store.add(f"https://www.rts.ch/info/suisse/{lettre}-article.html")
    store.write()

    dossier = tmp_path / "www.rts.ch/info/suisse"
    shards = sorted(p.name for p in dossier.glob("_index*.txt"))
    assert shards == ["_index.a.txt", "_index.b.txt", "_index.c.txt", "_index.d.txt", "_index.txt"]
    # `./` reste dans _index.txt, qui fait office d'en-tête.
    assert (dossier / "_index.txt").read_text(encoding="utf-8") == "./\n"
    assert (dossier / "_index.a.txt").read_text(encoding="utf-8") == "a-article.html\n"

    # Le sharding est réversible : repasser sous le seuil regroupe les fichiers.
    monkeypatch.setattr(config, "SHARD_THRESHOLD", 5_000)
    Store(tmp_path).load().write()
    assert sorted(p.name for p in dossier.glob("_index*.txt")) == ["_index.txt"]


def test_purge_des_dossiers_disparus(tmp_path):
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add("https://www.rts.ch/obsolete/x.html")
    store.write()
    assert (tmp_path / "www.rts.ch/obsolete").is_dir()

    store = Store(tmp_path)  # sans load() : l'index repart de zéro
    store.add(ARTICLE)
    store.write()
    assert not (tmp_path / "www.rts.ch/obsolete").exists()


def test_url_trop_longue_ignoree_sans_faire_echouer_le_run(tmp_path):
    """Incident réel : une page Play au slug de plusieurs centaines de
    caractères (``play/tv/19h30/video/<slug-phrase-entiere>/``) a fait planter
    un crawl de 350 pages, perdant tout le travail déjà accompli faute d'être
    rattrapée. add() doit désormais journaliser et continuer."""
    trop_longue = "https://www.rts.ch/play/tv/19h30/video/" + ("mot-" * 70) + "/"

    store = Store(tmp_path)
    store.add(RUBRIQUE)
    ajoutee = store.add(trop_longue)
    assert ajoutee is False

    stats = store.write()
    assert stats["urls"] == 1  # seule RUBRIQUE a survécu
    assert dict(Store(tmp_path).load().urls()) == {RUBRIQUE: False}

    lignes = (tmp_path / config.ANOMALIES_FILE).read_text(encoding="utf-8").splitlines()
    assert any(l.startswith("trop_long\t" + trop_longue) for l in lignes)


def test_collision_de_casse_journalisee_et_ignoree(tmp_path):
    """Une collision doit être signalée mais ne doit plus faire échouer le run
    en cours : sur un crawl de longue durée, perdre tout le travail pour un cas
    qui se compte à l'unité coûte bien plus cher que de l'ignorer."""
    store = Store(tmp_path)
    store.add("https://www.rts.ch/Info/x.html")
    ajoutee = store.add("https://www.rts.ch/info/y.html")
    assert ajoutee is False

    stats = store.write()
    assert stats["urls"] == 1  # seule la première URL a été indexée
    lignes = (tmp_path / config.ANOMALIES_FILE).read_text(encoding="utf-8").splitlines()
    assert any(l.startswith("collision\thttps://www.rts.ch/info/y.html\t") for l in lignes)


def test_majuscule_consignee_dans_les_anomalies(tmp_path):
    store = Store(tmp_path)
    store.add("https://www.rts.ch/Info/x.html")
    store.write()

    lignes = (tmp_path / config.ANOMALIES_FILE).read_text(encoding="utf-8").splitlines()
    assert lignes[0] == "type\turl\tdetail"
    assert lignes[1].startswith("majuscule\thttps://www.rts.ch/Info/x.html\t")

    # L'anomalie survit au rechargement, ce qui permet de détecter la collision
    # d'un run à l'autre alors que le disque a perdu la casse d'origine.
    relu = Store(tmp_path).load()
    assert relu.add("https://www.rts.ch/info/y.html") is False


def test_fichier_index_corrompu_n_empeche_pas_le_chargement(tmp_path):
    """Un fichier abîmé (écriture interrompue, encodage invalide) ne doit pas
    rendre tout le dépôt inexploitable."""
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add(AUTRE)
    store.write()

    corrompu = tmp_path / DOSSIER / "_index.txt"
    corrompu.write_bytes(b"\xff\xfe\x00\xff invalide")

    relu = Store(tmp_path).load()
    assert dict(relu.urls()) == {}  # le dossier abîmé est ignoré, pas planté


def test_anomalies_corrompues_n_empechent_pas_le_chargement(tmp_path):
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.write()

    (tmp_path / config.ANOMALIES_FILE).write_bytes(b"\xff\xfe garbage")

    relu = Store(tmp_path).load()
    assert dict(relu.urls()) == {ARTICLE: False}


def test_stats(tmp_path):
    store = Store(tmp_path)
    store.add_many([RUBRIQUE, ARTICLE, AUTRE])
    store.add(ARTICLE, dead=True)
    stats = store.write()

    assert stats["urls"] == 3
    assert stats["mortes"] == 1
    assert stats["vivantes_ou_non_verifiees"] == 2

    payload = json.loads((tmp_path / config.STATS_FILE).read_text(encoding="utf-8"))
    assert payload["par_hote"] == {"www.rts.ch": 3}
    assert "generated_at" in payload
