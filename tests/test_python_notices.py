"""Une redistribution doit conserver les textes et les avis des paquets inclus."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from collect_python_notices import collect


def distribution(tmp_path, files):
    for name, value in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    return SimpleNamespace(
        metadata={"Name": "example", "License-Expression": "MIT"},
        version="1.2.3", files=[Path(name) for name in files],
        locate_file=lambda file: tmp_path / file,
    )


def test_les_textes_et_avis_incorpores_sont_conserves(tmp_path):
    dist = distribution(tmp_path, {
        "example.dist-info/licenses/LICENSE": "texte intégral MIT",
        "example.dist-info/NOTICE": "attribution de l’auteur",
        "example/vendor/component/LICENSE.txt": "licence du composant incorporé",
        "example/licenses/parser.py": "ceci est du code",
    })
    manifest, notices = collect([dist])
    assert manifest[0]["name"] == "example"
    assert manifest[0]["version"] == "1.2.3"
    assert manifest[0]["source_distributions"] == "https://pypi.org/project/example/1.2.3/#files"
    for text in ("texte intégral MIT", "attribution de l’auteur", "licence du composant incorporé"):
        assert text in notices
    assert "ceci est du code" not in notices


@pytest.mark.parametrize("files", ({}, {"example.dist-info/LICENSE": " "}, {"example.dist-info/NOTICE": "attribution seule"}))
def test_une_licence_absente_ou_vide_bloque_la_distribution(tmp_path, files):
    with pytest.raises(ValueError, match="Licence absente.*example"):
        collect([distribution(tmp_path, files)])
