"""Manifeste de version : digests d'images immuables pour la chart.

``quadringent install`` n'accepte jamais de tag mouvant. Les digests des
trois images (lecteur/capture, control plane, vérificateur réutilisé pour
l'observabilité)
sont lus depuis un fichier JSON de manifeste, dont la forme est décrite par
``deploy/release-manifest.schema.json`` — par défaut
``deploy/release-manifest.example.json``,
un gabarit à valeurs factices (jamais un digest publié réel). Un site qui
possède un vrai registre fournit son propre fichier via
``--release-manifest``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")

DEFAULT_REPOSITORY_ENV = "QUADRINGENT_IMAGE_REPOSITORY"
DEFAULT_REPOSITORY_PLACEHOLDER = "ghcr.io/quadringent/quadringent"


class InvalidReleaseManifest(ValueError):
    pass


@dataclass(frozen=True)
class ReleaseManifest:
    repository: str
    image_digest: str
    control_plane_image_digest: str
    verifier_image_digest: str
    observability_image_digest: str

    @classmethod
    def from_file(cls, path: Path, *, repository_override: str | None = None) -> "ReleaseManifest":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise InvalidReleaseManifest(f"manifeste de version introuvable : {path}") from error
        except json.JSONDecodeError as error:
            raise InvalidReleaseManifest(f"manifeste de version invalide (JSON) : {path}") from error

        repository = repository_override or raw.get("repository") or DEFAULT_REPOSITORY_PLACEHOLDER
        digests = {
            "image_digest": raw.get("imageDigest", ""),
            "control_plane_image_digest": raw.get("controlPlaneImageDigest", ""),
            "verifier_image_digest": raw.get("verifierImageDigest", ""),
            "observability_image_digest": raw.get("observabilityImageDigest", ""),
        }
        for field_name, value in digests.items():
            if not _DIGEST_RE.match(value or ""):
                raise InvalidReleaseManifest(
                    f"{field_name} du manifeste doit être un digest sha256:<64 hex> (obtenu : {value!r})"
                )
        return cls(repository=repository, **digests)
