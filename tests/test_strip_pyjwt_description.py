"""Le retrait du README PyJWT conserve son contrat de distribution installé."""

import base64
import csv
import hashlib
from importlib.metadata import PackageNotFoundError, PathDistribution
from pathlib import Path

import pytest

from collect_python_notices import collect
import strip_pyjwt_description
from strip_pyjwt_description import strip_description


HEADER = (
    b"Metadata-Version: 2.4\nName: PyJWT\nVersion: 2.14.0\n"
    b"Summary: JSON Web Token implementation\nLicense-Expression: MIT\n"
    b"License-File: licenses/LICENSE\nRequires-Python: >=3.9\n"
    b"Requires-Dist: cryptography>=3.4; extra == 'crypto'\n"
    b"Requires-Dist: pytest; extra == 'tests'\n"
    b"Project-URL: Documentation, https://pyjwt.readthedocs.io\n\n"
)


def installed(tmp_path, metadata=HEADER + b"README de demonstration\n"):
    directory = tmp_path / "pyjwt-2.14.0.dist-info"
    directory.mkdir()
    (directory / "METADATA").write_bytes(metadata)
    (directory / "licenses").mkdir()
    (directory / "licenses/LICENSE").write_text("Texte integral MIT et attribution\n")
    package = tmp_path / "jwt"
    package.mkdir()
    (package / "__init__.py").write_text("__version__ = '2.14.0'\n")
    with (directory / "RECORD").open("w", newline="") as stream:
        writer = csv.writer(stream)
        for path in (directory / "METADATA", directory / "licenses/LICENSE", package / "__init__.py"):
            value = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).decode().rstrip("=")
            writer.writerow([path.relative_to(tmp_path).as_posix(), f"sha256={digest}", len(value)])
        writer.writerow(["pyjwt-2.14.0.dist-info/RECORD", "", ""])
    return PathDistribution(directory)


def test_description_seule_retiree_metadonnees_notices_et_record_coherents(tmp_path):
    dist = installed(tmp_path)
    before_notices = collect([dist])
    before_requires = dist.requires
    before_files = set(map(str, dist.files))
    untouched = {str(file): Path(dist.locate_file(file)).read_bytes() for file in dist.files
                 if file.name not in {"METADATA", "RECORD"}}
    strip_description(dist)
    refreshed = PathDistribution(tmp_path / "pyjwt-2.14.0.dist-info")
    assert refreshed.read_text("METADATA").encode() == HEADER
    assert refreshed.metadata["Name"] == "PyJWT"
    assert refreshed.version == "2.14.0"
    assert refreshed.requires == before_requires
    assert refreshed.metadata["Requires-Python"] == ">=3.9"
    assert refreshed.metadata["Summary"] == "JSON Web Token implementation"
    assert set(map(str, refreshed.files)) == before_files
    assert collect([refreshed]) == before_notices
    for file in refreshed.files:
        value = Path(refreshed.locate_file(file)).read_bytes()
        if file.hash:
            assert file.hash.mode == "sha256"
            assert file.hash.value == base64.urlsafe_b64encode(hashlib.sha256(value).digest()).decode().rstrip("=")
            assert file.size == len(value)
        if str(file) in untouched:
            assert value == untouched[str(file)]


def test_crlf_et_operation_repetee_preservent_entetes(tmp_path):
    header = HEADER.replace(b"\n", b"\r\n")
    dist = installed(tmp_path, header + b"description\r\n")
    strip_description(dist)
    strip_description(dist)
    assert Path(dist.locate_file("pyjwt-2.14.0.dist-info/METADATA")).read_bytes() == header


@pytest.mark.parametrize("metadata", [
    b"Name: PyJWT\nVersion: 2.14.0\nREADME sans separateur",
    HEADER.replace(b"Name: PyJWT", b"Name: unrelated"),
    HEADER.replace(b"Version: 2.14.0\n", b""),
    HEADER.replace(b"Metadata-Version: 2.4\n", b""),
    HEADER.replace(b"Name: PyJWT\n", b"Name: PyJWT\nName: PyJWT\n"),
    HEADER.replace(b"Summary: JSON Web Token implementation", b"entete invalide"),
])
def test_format_inattendu_refuse_sans_modifier(tmp_path, metadata):
    dist = installed(tmp_path, metadata)
    before = {file: Path(dist.locate_file(file)).read_bytes() for file in dist.files}
    with pytest.raises(ValueError, match="PyJWT"):
        strip_description(dist)
    assert all(Path(dist.locate_file(file)).read_bytes() == value for file, value in before.items())


@pytest.mark.parametrize("record", ["", "unrelated/METADATA,,\n", "pyjwt-2.14.0.dist-info/METADATA,,\n" * 2])
def test_record_absent_ou_ambigu_refuse_avant_modification(tmp_path, record):
    dist = installed(tmp_path)
    (tmp_path / "pyjwt-2.14.0.dist-info/RECORD").write_text(record)
    before = (tmp_path / "pyjwt-2.14.0.dist-info/METADATA").read_bytes()
    with pytest.raises(ValueError, match="PyJWT"):
        strip_description(dist)
    assert (tmp_path / "pyjwt-2.14.0.dist-info/METADATA").read_bytes() == before


def test_dockerfiles_nettoient_dans_meme_run_que_installation():
    root = Path(__file__).resolve().parents[1]
    for component in ("control-plane", "verifier"):
        dockerfile = (root / f"docker/{component}.Dockerfile").read_text()
        install_run = dockerfile.split("RUN pip install", 1)[1].split("\nWORKDIR", 1)[0]
        assert "\nRUN " not in install_run
        assert "&& python /tmp/strip_pyjwt_description.py" in install_run
        assert "&& rm /tmp/requirements.txt /tmp/strip_pyjwt_description.py" in install_run
        assert "COPY scripts/strip_pyjwt_description.py /tmp/strip_pyjwt_description.py" in dockerfile
        assert "!scripts/strip_pyjwt_description.py" in (root / f"docker/{component}.Dockerfile.dockerignore").read_text()


def test_distribution_absente_refuse_explicitement(monkeypatch):
    def absent(name):
        assert name == "PyJWT"
        raise PackageNotFoundError(name)

    monkeypatch.setattr(strip_pyjwt_description, "distribution", absent)
    with pytest.raises(ValueError, match="PyJWT.*distribution requise absente"):
        strip_pyjwt_description.main()


def test_autre_distribution_inchangee(tmp_path):
    dist = installed(tmp_path)
    unrelated = tmp_path / "unrelated-1.0.dist-info"
    unrelated.mkdir()
    path = unrelated / "METADATA"
    original = b"Metadata-Version: 2.4\nName: unrelated\nVersion: 1.0\n\nREADME conserve\n"
    path.write_bytes(original)
    strip_description(dist)
    assert path.read_bytes() == original
