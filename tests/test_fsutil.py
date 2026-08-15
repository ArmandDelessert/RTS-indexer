"""Tests de la résilience aux verrous transitoires du système de fichiers."""

import pytest

from rts_indexer import fsutil


def test_retry_reussit_apres_des_echecs_transitoires(monkeypatch):
    monkeypatch.setattr(fsutil.time, "sleep", lambda _: None)
    appels = {"n": 0}

    def action():
        appels["n"] += 1
        if appels["n"] < 3:
            raise PermissionError("verrouillé")
        return "ok"

    assert fsutil.retry(action, desc="test") == "ok"
    assert appels["n"] == 3


def test_retry_abandonne_apres_le_nombre_maximal_de_tentatives(monkeypatch):
    monkeypatch.setattr(fsutil.time, "sleep", lambda _: None)

    def toujours_verrouille():
        raise PermissionError("verrouillé")

    with pytest.raises(PermissionError):
        fsutil.retry(toujours_verrouille, desc="test")


def test_retry_ne_masque_pas_les_autres_erreurs(monkeypatch):
    """Un disque plein ou un fichier absent ne doit jamais être ré-essayé en
    boucle : seul un verrou transitoire (PermissionError) l'est."""
    monkeypatch.setattr(fsutil.time, "sleep", lambda _: None)
    appels = {"n": 0}

    def action():
        appels["n"] += 1
        raise FileNotFoundError("disparu")

    with pytest.raises(FileNotFoundError):
        fsutil.retry(action, desc="test")
    assert appels["n"] == 1  # aucune nouvelle tentative


def test_write_puis_read_text(tmp_path):
    path = tmp_path / "x.txt"
    fsutil.write_text(path, "contenu\n")
    assert fsutil.read_text(path) == "contenu\n"
    assert b"\r" not in path.read_bytes()


def test_unlink_silencieux_si_absent(tmp_path):
    fsutil.unlink(tmp_path / "n-existe-pas.txt")  # ne doit pas lever


# -- retry_many ----------------------------------------------------------


def test_retry_many_toutes_reussissent_du_premier_coup(monkeypatch):
    monkeypatch.setattr(fsutil.time, "sleep", lambda _: None)
    faits = []
    items = [(lambda i=i: faits.append(i), f"item-{i}") for i in range(5)]

    echecs = fsutil.retry_many(items)

    assert echecs == []
    assert sorted(faits) == [0, 1, 2, 3, 4]


def test_retry_many_regroupe_les_nouvelles_tentatives_par_lot(monkeypatch):
    """Le point du regroupement : un élément qui échoue une fois ne doit pas
    faire attendre les autres, et la tentative suivante doit reprendre le
    lot entier plutôt qu'un seul élément à la fois."""
    monkeypatch.setattr(fsutil.time, "sleep", lambda _: None)
    tentatives = {"a": 0, "b": 0}

    def fabrique(nom, echecs_avant_succes):
        def action():
            tentatives[nom] += 1
            if tentatives[nom] <= echecs_avant_succes:
                raise PermissionError("verrouillé")

        return action

    items = [(fabrique("a", 1), "a"), (fabrique("b", 2), "b")]
    echecs = fsutil.retry_many(items)

    assert echecs == []
    assert tentatives == {"a": 2, "b": 3}


def test_retry_many_signale_les_echecs_definitifs_sans_lever(monkeypatch):
    """Contrairement à retry(), qui lève : un échec de suppression de
    dossier vide ne doit jamais interrompre tout un run, mais ne doit pas
    non plus disparaître sans laisser de trace — c'est exactement ce que
    l'ancien code faisait (except OSError: break), rendant un taux
    d'échec de 100 % indiscernable d'une progression normale."""
    monkeypatch.setattr(fsutil.time, "sleep", lambda _: None)

    def toujours_verrouille():
        raise PermissionError("verrouillé")

    def reussit():
        pass

    items = [(toujours_verrouille, "coincé"), (reussit, "ok")]
    echecs = fsutil.retry_many(items)

    assert echecs == ["coincé"]  # ni levée, ni silence : rapporté


def test_retry_many_ne_masque_pas_les_autres_erreurs(monkeypatch):
    monkeypatch.setattr(fsutil.time, "sleep", lambda _: None)

    def disparu():
        raise FileNotFoundError("disparu")

    with pytest.raises(FileNotFoundError):
        fsutil.retry_many([(disparu, "x")])


def test_retry_many_liste_vide(monkeypatch):
    assert fsutil.retry_many([]) == []
