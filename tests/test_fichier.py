"""Tests de la source « fichier » : import d'URLs relevées à la main."""

from rts_indexer.sources import fichier

VALIDE = "https://www.rts.ch/galeries/2015/photo-1.html"
AUTRE = "https://www.rts.ch/info/suisse/"


def _ecrire(tmp_path, contenu):
    chemin = tmp_path / "urls.txt"
    chemin.write_text(contenu, encoding="utf-8")
    return chemin


def test_lit_une_url_par_ligne(tmp_path):
    chemin = _ecrire(tmp_path, f"{VALIDE}\n{AUTRE}\n")
    retenues, rejetees = fichier.lire(chemin)
    assert retenues == [VALIDE, AUTRE]
    assert rejetees == []


def test_ignore_les_lignes_vides_et_les_commentaires(tmp_path):
    contenu = f"# trouvé via Google, 2026-08\n\n{VALIDE}\n\n   \n# fin\n"
    retenues, rejetees = fichier.lire(_ecrire(tmp_path, contenu))
    assert retenues == [VALIDE]
    assert rejetees == []


def test_normalise_et_dedoublonne(tmp_path):
    """Deux écritures de la même URL ne doivent produire qu'une entrée."""
    contenu = f"{VALIDE}\n  {VALIDE}  \nhttp://www.rts.ch/info/suisse\n"
    retenues, _ = fichier.lire(_ecrire(tmp_path, contenu))
    assert retenues == [VALIDE, AUTRE]  # http -> https, slash final ajouté


def test_signale_les_lignes_rejetees(tmp_path):
    """Une faute de frappe doit se voir, pas disparaître en silence."""
    contenu = f"{VALIDE}\nhttps://www.srf.ch/hors-perimetre.html\npas-une-url\n"
    retenues, rejetees = fichier.lire(_ecrire(tmp_path, contenu))
    assert retenues == [VALIDE]
    assert rejetees == ["https://www.srf.ch/hors-perimetre.html", "pas-une-url"]


def test_fichier_vide(tmp_path):
    assert fichier.lire(_ecrire(tmp_path, "")) == ([], [])
