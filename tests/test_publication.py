"""La publication refuse les identités privées et les secrets réintroduits."""

import hashlib
from pathlib import Path
import subprocess

import pytest

from check_publication import load_private_hashes, scan_history, scan_text, scan_tree
from export_private import export


def test_un_identifiant_compose_reste_detecte_dans_un_schema() -> None:
    identity = "private_example"
    denied_hashes = frozenset({hashlib.sha256(identity.encode()).hexdigest()})
    assert scan_text("DATABASE." + identity.upper(), "example.sql", denied_hashes)
    assert scan_text("DATABASE." + identity.upper(), "example.sql") == []


def test_liste_privee_chargee_hors_du_code(tmp_path: Path) -> None:
    identity = "private_example"
    path = tmp_path / "denied.sha256"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    path.write_text(digest + "\n", encoding="ascii")
    assert load_private_hashes(path) == frozenset({digest})
    assert scan_text(identity, "example.sql", load_private_hashes(path))


@pytest.mark.parametrize("content", ["", "not-a-hash\n", "a" * 64 + "\n\n"])
def test_liste_privee_malformee_refusee(tmp_path: Path, content: str) -> None:
    path = tmp_path / "denied.sha256"
    path.write_text(content, encoding="ascii")
    with pytest.raises(ValueError, match="private denylist"):
        load_private_hashes(path)


def test_export_refuse_un_identifiant_present_dans_la_liste_externe(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    identity = "private_example"
    (source / "sample.txt").write_text(identity + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=source, check=True)
    denied_hashes = frozenset({hashlib.sha256(identity.encode()).hexdigest()})

    with pytest.raises(ValueError, match="Export refusé"):
        export(source, tmp_path / "public", denied_hashes)
    assert not (tmp_path / "public").exists()


@pytest.mark.parametrize("value", [
    "10." + "42.3.4", "172." + "20.1.2", "192." + "168.1.3",
    "arn:aws:iam::" + "123456" + "789012" + ":role/example",
    "quadringent1." + "a" * 60 + "." + "b" * 86,
], ids=["private-ip-10", "private-ip-172", "private-ip-192", "aws-account", "license-format"])
def test_refuse_une_valeur_interdite_sans_la_reproduire(value: str) -> None:
    findings = scan_text(value, "example.txt")
    assert findings
    assert value not in str(findings)


def test_accepte_les_adresses_documentaires_et_le_compte_neutre() -> None:
    assert scan_text("192.0.2.10 arn:aws:iam::000000000000:role/example", "example.txt") == []


def test_un_secret_dans_un_fichier_binaire_n_est_pas_ignore() -> None:
    assert scan_text("image\x00" + "quadringent1." + "a" * 60 + "." + "b" * 86, "image.png")


def test_un_chemin_local_ne_part_pas_dans_la_release() -> None:
    path = "/".join(("", "Users", "person", "Documents", "project", "source.py"))
    findings = scan_text(path, "example.txt")
    assert any(finding.rule == "local_home_path" for finding in findings)
    assert path not in str(findings)


def test_scan_arbre_ne_reproduit_pas_un_nom_de_fichier_prive(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    private_name = "10." + "42.3.4.txt"
    (tmp_path / private_name).write_text("neutral\n", encoding="utf-8")
    subprocess.run(["git", "add", private_name], cwd=tmp_path, check=True)

    findings = scan_tree(tmp_path)
    assert any(finding.rule == "private_ip" for finding in findings)
    assert private_name not in str(findings)


def test_l_arbre_a_publier_ne_contient_pas_d_identite_privee() -> None:
    assert scan_tree(Path(__file__).resolve().parents[1]) == []


def test_l_historique_refuse_une_identite_effacee_de_l_arbre(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "config", "user.name", "Qualification"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "qualification@example.invalid"], cwd=tmp_path, check=True)
    path = tmp_path / "example.txt"
    private_value = "10." + "42.3.4"
    path.write_text(f"source={private_value}\n", encoding="utf-8")
    subprocess.run(["git", "add", "example.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    path.write_text("source=192.0.2.10\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "sanitize"], cwd=tmp_path, check=True)

    assert scan_tree(tmp_path) == []
    findings = scan_history(tmp_path, "HEAD")
    assert any(finding.rule == "private_ip" for finding in findings)
    assert private_value not in str(findings)


def test_l_historique_sain_passe_sur_ref_isolee(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "config", "user.name", "Qualification"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "qualification@example.invalid"], cwd=tmp_path, check=True)
    (tmp_path / "example.txt").write_text("source=192.0.2.10\n", encoding="utf-8")
    subprocess.run(["git", "add", "example.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "sanitized"], cwd=tmp_path, check=True)

    assert scan_history(tmp_path, "HEAD") == []


def test_un_tag_annote_revele_une_identite_dans_son_message(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "config", "user.name", "Qualification"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "qualification@example.invalid"], cwd=tmp_path, check=True)
    (tmp_path / "example.txt").write_text("source=192.0.2.10\n", encoding="utf-8")
    subprocess.run(["git", "add", "example.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "sanitized"], cwd=tmp_path, check=True)
    private_value = "10." + "42.3.4"
    subprocess.run(["git", "tag", "-a", "v0.0.1", "-m", f"source={private_value}"],
                   cwd=tmp_path, check=True)

    assert scan_history(tmp_path, "HEAD") == []
    assert any(finding.rule == "private_ip" for finding in scan_history(tmp_path, "refs/tags/v0.0.1"))


def test_l_historique_controle_les_anciens_noms_de_fichier(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "config", "user.name", "Qualification"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "qualification@example.invalid"], cwd=tmp_path, check=True)
    old_name = "10." + "42.3.4.txt"
    (tmp_path / old_name).write_text("neutral\n", encoding="utf-8")
    subprocess.run(["git", "add", old_name], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    subprocess.run(["git", "mv", old_name, "example.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "rename"], cwd=tmp_path, check=True)

    assert scan_tree(tmp_path) == []
    findings = scan_history(tmp_path, "HEAD")
    assert any(finding.rule == "private_ip" for finding in findings)
    assert old_name not in str(findings)
