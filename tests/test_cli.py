"""Tests du CLI : surtout la résilience de `crawl` face à l'imprévu."""

import argparse
import io

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


def test_dedupe_supprime_et_rapporte(tmp_path, capsys):
    article = "https://www.rts.ch/info/suisse/2026/article/x-1.html"
    canonique = "https://www.rts.ch/info/suisse/2026/article/x-2.html"
    store = Store(tmp_path)
    store.add_many([article, canonique])
    store.anomalies.add(("doublon", article, canonique))
    store.write()

    code = cli.main(["--data-dir", str(tmp_path), "dedupe"])

    assert code == 0
    assert "1 doublons supprimés" in capsys.readouterr().out
    assert dict(Store(tmp_path).load().urls()) == {canonique: False}


def test_purge_supprime_et_rapporte(tmp_path, capsys):
    vivante = "https://www.rts.ch/info/suisse/2026/article/x-1.html"
    morte = "https://www.rts.ch/info/suisse/2026/article/x-2.html"
    store = Store(tmp_path)
    store.add_many([vivante, morte])
    store.add(morte, dead=True)
    store.write()

    code = cli.main(["--data-dir", str(tmp_path), "purge"])

    assert code == 0
    assert "1 URLs mortes supprimées" in capsys.readouterr().out
    assert dict(Store(tmp_path).load().urls()) == {vivante: False}


# -- import ----------------------------------------------------------------

GALERIE = "https://www.rts.ch/galeries/2015/photo-1.html"
CANONIQUE = "https://www.rts.ch/galeries/2015/photo-canon.html"


def test_import_ajoute_les_urls_du_fichier(tmp_path, capsys):
    liste = tmp_path / "urls.txt"
    liste.write_text(f"# trouvé via Google\n{GALERIE}\n", encoding="utf-8")

    code = cli.main(["--data-dir", str(tmp_path), "import", str(liste)])

    assert code == 0
    assert "1 nouvelles URLs" in capsys.readouterr().out
    assert dict(Store(tmp_path).load().urls()) == {GALERIE: False}


def test_import_dry_run_n_ecrit_rien(tmp_path, capsys):
    liste = tmp_path / "urls.txt"
    liste.write_text(f"{GALERIE}\n", encoding="utf-8")

    code = cli.main(["--data-dir", str(tmp_path), "import", str(liste), "--dry-run"])

    assert code == 0
    assert "--dry-run" in capsys.readouterr().out
    assert not (tmp_path / "www.rts.ch").exists()


def test_import_signale_les_lignes_rejetees(tmp_path, capsys):
    liste = tmp_path / "urls.txt"
    liste.write_text(f"{GALERIE}\npas-une-url\n", encoding="utf-8")

    cli.main(["--data-dir", str(tmp_path), "import", str(liste), "--dry-run"])

    sortie = capsys.readouterr().out
    assert "1 rejetées" in sortie
    assert "pas-une-url" in sortie


# -- tri des verdicts (import --check) -------------------------------------


def test_trier_verdicts_separe_vivantes_mortes_et_redirections():
    morte = "https://www.rts.ch/galeries/2015/morte.html"
    douteuse = "https://www.rts.ch/galeries/2015/douteuse.html"
    urls = [GALERIE, morte, douteuse]
    verdicts = {
        GALERIE: (301, CANONIQUE),
        morte: (404, morte),
        douteuse: (503, douteuse),
    }

    vivantes, mortes, redirigees, douteuses = cli._trier_verdicts(urls, verdicts)

    # La redirection fait retenir la cible, pas l'URL demandée.
    assert vivantes == [CANONIQUE]
    assert redirigees == [(GALERIE, CANONIQUE)]
    assert mortes == [(morte, 404)]
    assert douteuses == [(douteuse, 503)]


def test_trier_verdicts_echec_reseau_est_non_concluant():
    """Un échec réseau n'est pas une mort : la même prudence qu'ailleurs."""
    vivantes, mortes, _, douteuses = cli._trier_verdicts([GALERIE], {GALERIE: (None, None)})
    assert (vivantes, mortes) == ([], [])
    assert douteuses == [(GALERIE, None)]


def test_trier_verdicts_redirection_hors_perimetre_est_ecartee():
    """Rediriger vers une image sur img.rts.ch : ni l'URL ni sa cible n'ont
    leur place dans l'index."""
    image = "https://img.rts.ch/articles/2015/image/x-1.image"
    vivantes, _, redirigees, douteuses = cli._trier_verdicts([GALERIE], {GALERIE: (301, image)})
    assert (vivantes, redirigees) == ([], [])
    assert douteuses == [(GALERIE, 301)]


def test_trier_verdicts_url_saine_est_retenue_telle_quelle():
    vivantes, mortes, redirigees, douteuses = cli._trier_verdicts(
        [GALERIE], {GALERIE: (200, GALERIE)}
    )
    assert vivantes == [GALERIE]
    assert (mortes, redirigees, douteuses) == ([], [], [])


# -- anomalies -------------------------------------------------------------


def test_anomalies_inventorie_sans_rien_toucher(tmp_path, capsys):
    # Réellement trop longue : une URL courte serait écartée au rechargement
    # par _anomaly_still_applies, qui revérifie que le motif tient toujours.
    trop_longue = "https://www.rts.ch/play/tv/19h30/video/" + ("mot-" * 70) + "/"
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.add(trop_longue)  # rejetée, journalise l'anomalie elle-même
    store.anomalies.add(("hors_perimetre", ARTICLE, "https://img.rts.ch/x.image"))
    store.write()

    code = cli.main(["--data-dir", str(tmp_path), "anomalies"])

    sortie = capsys.readouterr().out
    assert code == 0
    assert "trop_long" in sortie
    assert "hors_perimetre" in sortie
    assert dict(Store(tmp_path).load().urls()) == {ARTICLE: False}  # rien touché


def test_anomalies_drop_out_of_scope_retire_sans_recontroler(tmp_path, capsys):
    """Pas de recontrôle réseau : le constat de `verify` fait déjà foi."""
    autre = "https://www.rts.ch/info/suisse/2026/article/y-2.html"
    store = Store(tmp_path)
    store.add_many([ARTICLE, autre])
    store.anomalies.add(("hors_perimetre", ARTICLE, "https://img.rts.ch/x.image"))
    store.write()

    code = cli.main(["--data-dir", str(tmp_path), "anomalies", "--drop-out-of-scope"])

    assert code == 0
    assert "1 URLs hors périmètre supprimées" in capsys.readouterr().out
    assert dict(Store(tmp_path).load().urls()) == {autre: False}
    assert Store(tmp_path).load().anomalies == set()


def test_anomalies_drop_out_of_scope_ignore_les_doublons(tmp_path, capsys):
    store = Store(tmp_path)
    store.add(ARTICLE)
    store.anomalies.add(("doublon", ARTICLE, "https://www.rts.ch/autre.html"))
    store.write()

    cli.main(["--data-dir", str(tmp_path), "anomalies", "--drop-out-of-scope"])

    assert dict(Store(tmp_path).load().urls()) == {ARTICLE: False}
    assert ("doublon", ARTICLE, "https://www.rts.ch/autre.html") in Store(tmp_path).load().anomalies


# -- run ---------------------------------------------------------------------


def test_run_enchaine_plusieurs_commandes_sur_un_seul_store(tmp_path, monkeypatch, capsys):
    """Le point de `run` : un seul load()/write() pour toute la chaîne, pas un
    par commande. On le vérifie en comptant les vrais appels à Store.write."""
    vivante = "https://www.rts.ch/info/suisse/2026/article/x-1.html"
    morte = "https://www.rts.ch/info/suisse/2026/article/x-2.html"
    store = Store(tmp_path)
    store.add_many([vivante, morte])
    store.add(morte, dead=True)
    store.anomalies.add(("doublon", morte, vivante))
    store.write()

    appels = []
    original = Store.write

    def write_compte(self, **kwargs):
        appels.append(1)
        return original(self, **kwargs)

    monkeypatch.setattr(Store, "write", write_compte)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("purge\ndedupe\n"))

    code = cli.main(["--data-dir", str(tmp_path), "run"])

    assert code == 0
    assert len(appels) == 1  # une seule écriture pour les deux commandes
    assert dict(Store(tmp_path).load().urls()) == {vivante: False}  # morte purgée


def test_run_lit_depuis_un_fichier(tmp_path):
    store = Store(tmp_path)
    store.add("https://www.rts.ch/info/suisse/2026/article/x-1.html")
    store.write()

    liste = tmp_path / "commandes.txt"
    liste.write_text("# un commentaire\nstats\n", encoding="utf-8")

    code = cli.main(["--data-dir", str(tmp_path), "run", "--file", str(liste)])
    assert code == 0


def test_run_s_arrete_a_la_premiere_commande_en_echec(tmp_path, monkeypatch, capsys):
    """`crawl` sur un index sans rubrique renvoie 1 : la chaîne doit s'arrêter
    là, pas continuer sur les commandes suivantes."""
    store = Store(tmp_path)
    store.write()  # index vide, sans rubrique

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("crawl\nstats\n"))
    code = cli.main(["--data-dir", str(tmp_path), "run"])

    assert code == 1
    assert "Aucune rubrique" in capsys.readouterr().err


def test_run_ignore_une_ligne_invalide_et_continue(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path)
    store.add("https://www.rts.ch/info/suisse/2026/article/x-1.html")
    store.write()

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("--option-inconnue\nstats\n"))
    code = cli.main(["--data-dir", str(tmp_path), "run"])

    assert code == 0
    assert "commande invalide, ignorée" in capsys.readouterr().err


def test_run_sans_commandes_est_une_erreur(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.write()
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n# rien que des commentaires\n"))
    assert cli.main(["--data-dir", str(tmp_path), "run"]) == 1


def test_run_imbrique_est_refuse(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path)
    store.write()
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("run\n"))
    code = cli.main(["--data-dir", str(tmp_path), "run"])
    assert code == 1
    assert "imbriqué" in capsys.readouterr().err
