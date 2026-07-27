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
