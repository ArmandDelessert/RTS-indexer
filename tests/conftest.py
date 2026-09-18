"""Outillage commun aux tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rts_indexer import profiles


@pytest.fixture
def profil():
    """Active, le temps du test, un profil dérivé du profil par défaut.

    Les réglages qui dépendent du site indexé (débit poli, seuils de stockage,
    périmètre) vivent dans un :class:`~rts_indexer.profiles.Profile` figé : on
    n'en modifie pas un attribut, on en dérive un autre.
    ``monkeypatch.setattr`` échouerait — la dataclasse est ``frozen``.

        def test_x(profil):
            profil(shard_threshold=3)
    """
    piles = []

    def _activer(**champs) -> profiles.Profile:
        contexte = profiles.use(replace(profiles.load(profiles.DEFAULT_PROFILE), **champs))
        piles.append(contexte)
        return contexte.__enter__()

    yield _activer
    for contexte in reversed(piles):
        contexte.__exit__(None, None, None)
