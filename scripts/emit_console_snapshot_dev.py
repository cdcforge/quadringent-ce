#!/usr/bin/env python3
"""Produit un document de console en faisant tourner la vraie boucle de capture.

Sur une source **fictive**, en mémoire, sans IBM i, sans AWS et sans Snowflake.
Le but n'est pas de mesurer quoi que ce soit — c'est de donner à l'interface un
document réellement produit par ``ConsoleSnapshotBuilder``, plutôt qu'un JSON
écrit à la main qui divergerait du jour où le contrat bouge.

Le flux s'appelle ``dev-loopback`` et sa cible est « aucune — boucle locale » :
rien dans ce document ne peut être confondu avec une mesure de production.

Le journal simulé traverse quatre régimes, parce que ce sont ceux que l'écran
doit savoir distinguer :

    1. un retard initial que la boucle résorbe   -> le plancher s'effondre
    2. une rotation de receiver franchie          -> le compteur avance
    3. une tenue au tail                          -> le plancher reste bas
    4. une pointe de production absorbée          -> un pic, puis retour au tail

    python3 scripts/emit_console_snapshot_dev.py --out ui/fixtures/console-dev.json
"""

from __future__ import annotations

# Preuve historique : ce script émet un rapport daté d'une campagne passée.
# Les identifiants d'installation qu'il contient (compte, bucket, schémas,
# tables, récepteurs, digests d'image) sont le contenu figé de cette preuve,
# pas la configuration du produit — ils ne doivent pas être paramétrés ni
# réutilisés comme valeurs par défaut.

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src",):
    if str(ROOT / extra) not in sys.path:
        sys.path.insert(0, str(ROOT / extra))

from quadringent.checkpoint import JsonCheckpointStore  # noqa: E402
from quadringent.console_snapshot import (  # noqa: E402
    ConsoleSnapshotBuilder,
    FileSnapshotSink,
    FluxIdentity,
)
from quadringent.continuous import (  # noqa: E402
    CapturedWindow,
    ContinuousCaptureService,
    ReceiverSnapshot,
)
from quadringent.contract import ChangeEvent, JournalPosition  # noqa: E402
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator  # noqa: E402
from quadringent.raw import RawBatchWriter  # noqa: E402

IDENTITY = FluxIdentity(
    id="dev-loopback",
    label="Journal de démonstration — boucle locale",
    journal="DEVJRN",
    journal_library="DEVLIB",
    objects=("SALE",),
    reader_path="RetrieveJournal",
    target="aucune — boucle locale",
    job="emit_console_snapshot_dev",
)

FIRST_RECEIVER = "DEVJRN0001"
SECOND_RECEIVER = "DEVJRN0002"
ROTATION_SEQUENCE = 200_000  # dernière séquence du premier receiver
BACKLOG = 260_000
TAIL_POLLS = 120
SPIKE_AT = 160
SPIKE_SEQUENCES = 48_000


class SimulatedJournal:
    """Un journal qui produit des séquences, et un catalogue qui les publie.

    La boucle de capture ne sait pas qu'elle est simulée : elle lit un
    catalogue, planifie une fenêtre, la capture et avance son checkpoint,
    exactement comme en production.
    """

    def __init__(self, scratch: Path) -> None:
        self.tail = BACKLOG
        self.polls = 0
        self.writer = RawBatchWriter(scratch)
        self.scratch = scratch

    def snapshot(self) -> list[ReceiverSnapshot]:
        # Deux receivers contigus, sans trou de séquence : le franchissement
        # doit passer par un CAS sur le prédécesseur exact, comme en vrai.
        return [
            ReceiverSnapshot("DEVLIB", FIRST_RECEIVER, 0, ROTATION_SEQUENCE),
            ReceiverSnapshot("DEVLIB", SECOND_RECEIVER, ROTATION_SEQUENCE + 1, self.tail),
        ]

    def capture(self, window) -> CapturedWindow:
        # Une fenêtre sur cinq ne contient aucune ligne de l'objet suivi : le
        # scan vide est un état normal, et l'écran doit savoir le compter.
        if self.polls % 5 == 4:
            return CapturedWindow(scanned_to=window.end)

        span = window.end.sequence - window.start.sequence
        step = max(1, span // 8)
        sequences = list(range(window.start.sequence, window.end.sequence + 1, step))[:8]
        if not sequences:
            return CapturedWindow(scanned_to=window.end)

        manifest = self.writer.write_batch(
            [_event(window.end.receiver, sequence) for sequence in sequences],
            high_watermark=window.end,
        )
        return CapturedWindow(
            scanned_to=window.end,
            manifest=(self.scratch / f"batch-{manifest.batch_id}.manifest.json").read_bytes(),
            payload=(self.scratch / f"batch-{manifest.batch_id}.jsonl").read_bytes(),
        )

    def produce(self) -> None:
        """La source avance entre deux polls, comme un vrai journal."""

        self.polls += 1
        self.tail += SPIKE_SEQUENCES if self.polls == SPIKE_AT else 40


def _event(receiver: str, sequence: int) -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi",
        journal="DEVJRN",
        library="DEVLIB",
        table="SALE",
        operation="c",
        position=JournalPosition(receiver, sequence),
        commit_timestamp="2026-08-28T10:00:00Z",
        schema_version="sha256:dev-loopback",
        before=None,
        after={"ID": sequence},
    )


class MonotonicClock:
    """Horloge de simulation : un poll vaut une seconde, sans attendre."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def tick(self, seconds: float = 1.0) -> None:
        self.value += seconds


def run(out: Path, workspace: Path) -> dict[str, object]:
    journal = SimulatedJournal(workspace / "scratch")
    clock = MonotonicClock()
    checkpoint = JsonCheckpointStore(workspace / "checkpoint.json")
    coordinator = RawFirstCaptureCoordinator(
        FileObjectStore(workspace / "raw"), checkpoint
    )
    service = ContinuousCaptureService(
        journal,
        journal,
        coordinator,
        checkpoint,
        max_entries=5_000,
        catch_up_max_entries=20_000,
        bootstrap=JournalPosition(FIRST_RECEIVER, 0),
        poll_seconds=0.0,
        sleep=lambda _seconds: None,
    )
    console = ConsoleSnapshotBuilder(
        identity=IDENTITY, clock=clock, _started_monotonic=clock()
    )

    def report(result, metrics: dict[str, object]) -> None:
        clock.tick()
        console.observe(result, metrics)
        journal.produce()

    lag = None
    while True:
        service.run(max_polls=1, on_result=report)
        lag = service.metrics.last_lag_sequences
        caught_up = lag is not None and lag <= 1
        if caught_up and journal.polls > SPIKE_AT + TAIL_POLLS:
            break
        if journal.polls > 4_000:  # garde-fou : la simulation doit converger
            break

    console.mark_stopped("STOPPED_BUDGET", "fin de la simulation")
    console.observe_cpu_seconds(clock() * 0.03)
    document = console.document()
    FileSnapshotSink(out).write(console.encode())
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="ui/fixtures/console-dev.json")
    parser.add_argument(
        "--workspace",
        default=None,
        help="répertoire de travail ; par défaut un temporaire jeté à la fin",
    )
    args = parser.parse_args()

    out = Path(args.out)
    if args.workspace:
        document = run(out, Path(args.workspace))
    else:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            document = run(out, Path(directory))

    lag = document["lag"]
    print(
        f"{out} — {len(lag['series']['buckets'])} seaux, "
        f"{lag['series']['sample_count']} échantillons, "
        f"verdict {lag['verdict']['value']}, "
        f"plancher {lag['floor_first_third']['value']} -> "
        f"{lag['floor_last_third']['value']}, "
        f"pic {lag['max']['value']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
