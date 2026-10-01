"""Protocole de bascule journal (« boundary protocol »), chantier 4 §2.

Contrat produit (`docs/plans/2026-09-23-produit-fini-design.md` §2) : « position
du journal, copie initiale cohérente, bascule exacte sur le journal (un
bootstrap explicite n'est jamais reculé) ».

## Protocole retenu

1. **Lecture de la position** — une seule requête JDBC
   (`QSYS2.JOURNAL_RECEIVER_INFO`, cf. `ReadOnlyReceiverCatalog.java`) lit
   *dans la même fenêtre de transaction* le receiver attaché et sa dernière
   séquence (`ATTACH_TIMESTAMP` ordonné, comme le fait déjà le catalogue
   receveur réel). C'est la position de bascule : elle est prise **avant**
   le lancement de la copie, jamais après — c'est l'ordre du design (§2 :
   position, puis copie, puis bascule) et c'est la seule option qui rend la
   bascule *indépendante de la durée de la copie* (une copie longue ne
   retarde jamais le début de la capture continue).
2. **Copie initiale cohérente** — `ReadOnlyTableSnapshot` (Java) avec un
   identifiant de run UUID et un répertoire de sortie vide (l'échec est
   immédiat sinon, cf. `ReadOnlyTableSnapshot.java:41`). La copie n'est
   *pas* filtrée par la position : elle lit l'état courant de la table,
   quelle que soit sa durée.
3. **Bascule explicite** — la capture continue démarre à
   `sequence + 1` du receiver lu à l'étape 1, jamais recalculée après coup :
   `plan_bootstrap` ne fait qu'valider qu'aucune régression n'a eu lieu
   entre la lecture et l'enregistrement (horloge de contrôle, pas une
   nouvelle lecture) — le bootstrap une fois choisi est un fait immuable
   (colonne `bootstrap_receiver`/`bootstrap_sequence` de la preuve).

## Risque résiduel

Toute ligne modifiée entre l'instant de la lecture de position (étape 1) et
la fin de la copie (étape 2) peut être capturée par la copie dans un état
partiel ou incohérent avec la modification — la copie n'est pas isolée de
ces écritures concurrentes par un verrou. Deux garanties compensent ce
risque, posées ailleurs dans le produit et rappelées ici pour que
l'exécuteur les respecte :

- **Historique (brut durable + Snowpipe Streaming)** : chaque évènement
  journal a une clé naturelle `(receiver, sequence)` ; la capture continue,
  qui commence exactement à `sequence + 1`, rejoue tout évènement survenu
  pendant la copie sans jamais le dupliquer (dédoublonnage déjà en place
  côté chargeur, `snowflake_loader.py`).
- **Miroir (MERGE par clé)** : la ligne écrite par la copie initiale est
  taguée avec une position synthétique strictement antérieure à toute
  position réelle capturée ensuite pour la même table
  (`bootstrap_sequence`, jamais un `sequence` de capture réelle qui lui
  soit inférieur ou égal) ; le MERGE applique toujours la ligne dont la
  position est la plus grande. Une modification survenue pendant la copie
  est donc *toujours* rattrapée par l'évènement journal correspondant, qui
  écrase la valeur (potentiellement obsolète ou partielle) déposée par la
  copie — jamais l'inverse. Le miroir converge vers l'état correct dès que
  la capture continue a rattrapé son retard, sans dépendre de la durée de
  la copie ni d'une synchronisation fine entre copie et capture.

Ce protocole ne requiert donc pas que la source soit immobile pendant la
copie ; il requiert seulement que la lecture de position (étape 1) soit
prise avant que la copie ne commence, et que le MERGE respecte l'ordre par
position. C'est le choix le plus sûr réalisable avec les facilités IBM i du
produit (JDBC, pas de verrou de table applicatif, pas de fenêtre de
maintenance imposée à l'utilisateur).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


class BoundaryError(ValueError):
    """Erreur de base du protocole de bascule."""


class BoundaryRegressionError(BoundaryError):
    """Une bascule enregistrée ne peut jamais être reculée.

    Levée si l'appelant tente d'enregistrer un bootstrap dont la séquence
    est strictement inférieure à un bootstrap déjà enregistré pour la même
    table, ou dont le receiver ne descend pas d'une rotation cohérente
    (retour à un receiver antérieur).
    """


@dataclass(frozen=True)
class JournalBoundary:
    """Position du journal lue en une seule requête (receiver + séquence).

    ``observed_at`` est l'horloge du control plane au moment de la lecture,
    conservée pour l'audit et la preuve — jamais utilisée pour recalculer la
    position.
    """

    receiver_library: str
    receiver_name: str
    last_sequence: int
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.receiver_library.strip() or not self.receiver_name.strip():
            raise BoundaryError("bibliothèque et nom de receiver requis")
        if self.last_sequence < 0:
            raise BoundaryError("la séquence de bascule ne peut pas être négative")
        if self.observed_at.tzinfo is None:
            raise BoundaryError("l'horodatage de bascule doit être aware (UTC)")

    @property
    def bootstrap_sequence(self) -> int:
        """La capture continue démarre strictement après cette position."""

        return self.last_sequence

    def to_dict(self) -> dict[str, object]:
        return {
            "receiver_library": self.receiver_library,
            "receiver_name": self.receiver_name,
            "last_sequence": self.last_sequence,
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "JournalBoundary":
        observed_at = payload["observed_at"]
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at)
        return cls(
            receiver_library=str(payload["receiver_library"]),
            receiver_name=str(payload["receiver_name"]),
            last_sequence=int(payload["last_sequence"]),
            observed_at=observed_at,
        )


def assert_no_regression(
    previous: JournalBoundary | None, candidate: JournalBoundary
) -> None:
    """Refuse d'enregistrer un bootstrap qui reculerait la bascule.

    Sans bootstrap précédent (première copie, ou ``restart_initial_copy``
    qui repart délibérément d'une nouvelle lecture), tout candidat est
    accepté. Avec un bootstrap précédent, seules deux évolutions sont
    sûres : rester sur le même receiver avec une séquence non décroissante,
    ou passer à un receiver dont l'horodatage de bascule est postérieur (une
    rotation ne recule jamais l'horloge du journal). Un même receiver avec
    une séquence plus petite, ou un retour à un receiver antérieur, est une
    régression — signe d'une mauvaise lecture ou d'une manipulation du
    journal — et doit interrompre l'automatisme (état ``attention``).
    """

    if previous is None:
        return
    if candidate.receiver_library == previous.receiver_library and (
        candidate.receiver_name == previous.receiver_name
    ):
        if candidate.last_sequence < previous.last_sequence:
            raise BoundaryRegressionError(
                "la séquence de bascule ne peut pas reculer sur le même receiver"
            )
        return
    if candidate.observed_at < previous.observed_at:
        raise BoundaryRegressionError(
            "un changement de receiver ne peut pas reculer l'horodatage de bascule"
        )


def plan_bootstrap(
    boundary: JournalBoundary, *, previous: JournalBoundary | None = None
) -> JournalBoundary:
    """Valide et retourne la position à enregistrer comme bootstrap explicite.

    Ne relit jamais le journal : c'est une validation pure de la position
    déjà lue (étape 1 du protocole), jamais un recalcul après la copie.
    """

    assert_no_regression(previous, boundary)
    return boundary
