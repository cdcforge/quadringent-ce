# Quadringent produit fini — plan d’implémentation

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** livrer le produit décrit dans `2026-09-23-produit-fini-design.md` :
installation en une commande (AWS/GCP, VM/cluster), assistant en trois écrans,
démarrage automatique, pilotage complet, agent first, latence de quelques
secondes et qualification continue.

**Architecture:** un seul moteur (chart Helm + images) ; control plane `/v2`
consommé par l’UI, la CLI et le MCP ; lecteurs Kubernetes par connexion ;
brut durable S3/GCS puis Snowpipe Streaming vers historique et miroir.

**Tech Stack:** Python 3.12+ (control plane, orchestration), Java 21 + JTOpen
(lecteur IBM i), React/Vite (UI), Helm 3, Terraform, Snowflake (Snowpipe
Streaming), S3/DynamoDB, GCS, k3s, EKS, GKE.

**Méthode :** chaque chantier est détaillé en tâches TDD au moment où il
commence, à partir des décisions du chantier précédent. Seul le chantier 1 est
détaillé ici : les suivants dépendent de ses mesures (latence, queue vivante,
miroir). Aucun délai n’est engagé.

---

## Feuille de route

| # | Chantier | Entrée | Sortie vérifiable |
|---|---|---|---|
| 1 | Moteur et spikes de latence | Branche actuelle | Défauts moteur corrigés ; décisions queue vivante, fraîcheur du journal et miroir consignées ; latence mesurée de bout en bout |
| 2 | Destination Snowflake | Décision miroir (1.7) | Historique en Snowpipe Streaming et miroir typé créés automatiquement depuis le catalogue ; rapprochement trois voies PASS |
| 3 | Control plane `/v2` | 2 | API OpenAPI complète (état, actions à tous niveaux, `dry_run`, idempotence, erreurs à action suivante), Postgres, rôles, jetons d’agent, confirmations, audit, SSE ; MCP intégré ; CLI JSON |
| 4 | Orchestration et démarrage automatique | 3 | Découverte des tables et état de journalisation, commandes CL générées, copie initiale → bascule exacte → streaming par table, pause/reprise à tous niveaux, lecteurs par connexion |
| 5 | UI | 3, 4 | Système visuel (Plex, bandes, raccourcis F), activation admin, assistant trois écrans, vues trois niveaux, journaux, métriques et coûts en direct ; parcours rejoué en navigateur |
| 6 | Déploiement | 3, 4 | Chart avec `values.schema.json` complet, images multi-arch signées publiques, CLI `quadringent install` + modules Terraform AWS/GCP × VM/cluster |
| 7 | Qualification continue | 1 à 6 | Harnais nocturne S3 et GCS, matrice d’installation à chaque version, assistant piloté par agent via MCP, tableau de bord des runs |

Le harnais privé de qualification (hors dépôt) reste l’oracle d’acceptation
jusqu’au chantier 7, qui en reprend une version générique dans le dépôt.

---

## Chantier 1 — Moteur et spikes de latence

**Avancement (23 septembre 2026)** : 1.1 fait (`290f312`) ; 1.2 fait (`3168483`) ;
1.3 tranchée par mesure et implémentée avec 1.4 (`73baec5`, décision
`docs/decisions/2026-09-23-queue-vivante.md` : la règle de queue vivante est
retirée, la dernière entrée d’un receiver attaché est lue, y compris après une
transition). Les textes des tâches 1.3 et 1.4 ci-dessous décrivent l’intention
initiale ; la décision fait foi. 1.5 et 1.6 en cours.

Commandes communes, depuis la racine du dépôt :

- Python : `.venv/bin/python -m pytest -q <cibles>`
- Tout Python : `.venv/bin/python -m pytest -q` (attendu : 0 échec, 2 skips hérités)
- Java : `sh scripts/test_java_all.sh` (attendu : 10 programmes PASS)
- Lint : `.venv/bin/ruff check src scripts tests`

### Task 1.1 : refuser un budget inférieur au délai du lecteur

Aujourd’hui `--max-seconds` inférieur ou égal à `AS400_READER_TIMEOUT_SECONDS`
(300 s par défaut) termine la capture sans aucun poll et sans erreur.

**Files:**
- Modify: `scripts/as400_continuous_capture.py` (après `args = parser.parse_args()`)
- Test: `tests/test_capture_budget.py`

**Step 1: Write the failing test**

```python
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import as400_continuous_capture as capture
from test_continuous_capture_gcs import ENV


class CaptureBudgetTests(unittest.TestCase):
    def test_budget_not_above_reader_timeout_is_refused_before_io(self) -> None:
        for budget, timeout in (("60", "300"), ("30", "30")):
            with self.subTest(budget=budget), patch.dict(
                os.environ, {**ENV, "AS400_READER_TIMEOUT_SECONDS": timeout}, clear=True
            ), patch("sys.argv", ["capture", "--max-seconds", budget]), patch.object(
                capture, "PersistentJavaWorker"
            ) as worker, patch.object(capture, "_gcs_client") as client:
                with self.assertRaisesRegex(ValueError, "max-seconds"):
                    capture.main()
            worker.assert_not_called()
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

**Step 2:** `.venv/bin/python -m pytest -q tests/test_capture_budget.py` → FAIL (aucune erreur levée).

**Step 3: Minimal implementation** — juste après `args = parser.parse_args()` :

```python
    if args.max_seconds is not None and args.max_seconds <= _reader_timeout_seconds():
        # Sinon la boucle s'arrête avant le premier poll, sans rien signaler.
        raise ValueError("--max-seconds must exceed AS400_READER_TIMEOUT_SECONDS")
```

**Step 4:** relancer le test → PASS ; puis `tests/test_continuous_capture_tables.py tests/test_worker_proof_window.py tests/test_continuous_capture_gcs.py` → PASS (ajuster seulement les tests qui passaient un budget incohérent, sans changer leur intention).

**Step 5:** commit `fix: refuse a capture budget that cannot fit one reader window`.

### Task 1.2 : un bootstrap explicite n’est jamais reculé

`plan_next_window` recule un bootstrap égal à `last_sequence` jusqu’à
`last_sequence - max_entries + 1`. Seul `__TAIL__` (via `finite_tail_bootstrap`,
qui calcule déjà sa position) justifie un recul.

**Files:**
- Modify: `src/quadringent/continuous.py` (`plan_next_window`, branche `checkpoint is None`)
- Modify: `tests/test_continuous.py` (`test_tail_last_sequence_bootstraps_a_finite_lookback_not_a_one_seq_wait`)
- Test: `tests/test_continuous.py`

**Step 1: Write the failing test** (ajouter à la classe de tests de planification) :

```python
    def test_explicit_bootstrap_is_never_moved_backwards(self) -> None:
        cases = [
            [ReceiverSnapshot("QGPL", "R2", 100, 110, status="ONLINE")],
            [ReceiverSnapshot("QGPL", "R2", 100, 110, status="ATTACHED")],
            [ReceiverSnapshot("QGPL", "R2", 100, 111, status="ATTACHED")],
        ]
        for receivers in cases:
            with self.subTest(receivers=receivers):
                plan = plan_next_window(None, receivers, max_entries=11,
                                        bootstrap=JournalPosition("R2", 110))
                if plan is not None:
                    self.assertEqual(plan.start, JournalPosition("R2", 110))
```

Réécrire le test existant : `finite_tail_bootstrap` rend toujours
`JournalPosition("R2", 100)` ; `plan_next_window` avec bootstrap 110 sur un
receiver `ATTACHED` 100..110 rend `None` (attente) tant que la tâche 1.3 n’a pas
changé la règle de queue vivante.

**Step 2:** exécuter `tests/test_continuous.py` → le nouveau test échoue pour le cas `ATTACHED` 100..110 (start = 100).

**Step 3: Implementation** — supprimer le bloc :

```python
        if (
            start.sequence == current.last_sequence
            and current.first_sequence < current.last_sequence
        ):
            ...
```

**Step 4:** `tests/test_continuous*.py` → PASS ; suite complète → PASS.

**Step 5:** commit `fix: never move an explicit bootstrap backwards`.

### Task 1.3 : spike queue vivante (décision)

La règle « ne jamais lire `last_sequence` d’un receiver `ATTACHED` » est
justifiée par un commentaire sur RetrieveJournal. La lecture SQL
`DISPLAY_JOURNAL` et RetrieveJournal doivent être mesurées séparément.

**Steps:**
1. Sur l’IBM i de qualification, table de test journalisée, une seule écriture
   isolée : lire la fenêtre `[last, last]` du receiver attaché par
   `DISPLAY_JOURNAL` puis par RetrieveJournal (`JournalSession.processWindow`) ;
   répéter 20 fois avec écritures espacées ; consigner entrées complètes,
   partielles, erreurs et délais.
2. Rechercher dans la documentation IBM (Context7 ou pages IBM) le comportement
   de `QjoRetrieveJournalEntries` sur la dernière entrée d’un receiver attaché.
3. Écrire `docs/decisions/2026-xx-queue-vivante.md` : règle retenue par chemin
   de lecture, preuves, risques.
4. Si la lecture est sûre pour un chemin : tâche TDD dédiée qui modifie
   `_is_attached_live_tail` et `_window`, adapte
   `test_attached_receiver_window_ends_strictly_before_live_last_sequence` et
   `test_attached_tail_is_idle_when_cursor_reaches_live_last_sequence`, et
   ajoute un test « écriture isolée visible au poll suivant ».

**Sortie :** une modification isolée est capturée sans attendre la suivante, ou
une limite documentée et visible dans le cockpit.

### Task 1.4 : receiver attaché scanné après rotation

Observé : après deux rotations, la fenêtre `169..169` du receiver attaché
`first = last = 169` a été lue (empty scan) malgré la règle de queue vivante.

**Files:** `src/quadringent/continuous.py` (branche de transition vers `following`), `tests/test_continuous.py`

**Step 1: Write the failing test** reproduisant l’état observé :

```python
    def test_transition_into_attached_receiver_respects_live_tail_rule(self) -> None:
        receivers = [
            ReceiverSnapshot("L", "R1", 1, 161, status="ONLINE"),
            ReceiverSnapshot("L", "R2", 162, 168, status="ONLINE"),
            ReceiverSnapshot("L", "R3", 169, 169, status="ATTACHED"),
        ]
        plan = plan_next_window(JournalPosition("R2", 168), receivers, max_entries=50)
        # Attendu selon la décision 1.3 : None tant que la queue vivante est exclue.
        self.assertIsNone(plan)
```

**Steps 2-5:** vérifier l’échec, corriger la branche de transition pour appliquer
la même règle que la tâche 1.3 (ou adapter l’attente du test à la décision),
suite complète, commit `fix: apply the live-tail rule after a receiver transition`.

### Task 1.5 : fraîcheur du journal sans catalogue complet

Le catalogue est réutilisé jusqu’à 60 s ou 30 polls
(`AS400_RECEIVER_CATALOG_CACHE_SECONDS`, `AS400_RECEIVER_CATALOG_CACHE_POLLS`) :
une nouvelle entrée peut rester invisible une minute. Un rafraîchissement
complet coûte environ 3,5 s par poll sur l’IBM i de qualification.

**Steps:**
1. Mesurer sur l’IBM i de qualification : coût d’une requête limitée au receiver
   attaché (`JOURNAL_RECEIVER_INFO` filtré sur `STATUS = 'ATTACHED'`), d’un
   `DISPLAY_JOURNAL` borné à partir du checkpoint, et du catalogue complet.
2. Concevoir une sonde de queue légère appelée à chaque poll ; le catalogue
   complet n’est relu que si la sonde voit un changement de receiver.
3. Tâche TDD : `CachedReceiverCatalog` accepte une sonde (`tail_probe`) ; tests
   « nouvelle entrée visible au poll suivant malgré le cache » et « changement de
   receiver invalide le cache » dans `tests/test_java_catalog.py` ; côté Java,
   commande `tail` du worker dans `PersistentJournalWorker` avec test hors ligne
   dans `JournalSessionTablesTest`.
4. Poll adaptatif : 1 s après une fenêtre non vide, croissance jusqu’à
   `AS400_POLL_SECONDS` au repos ; test sur `ContinuousCaptureService` avec
   horloge et `sleep` injectés.

**Sortie :** délai entre écriture et publication brute mesuré (p50, p95).

### Task 1.6 : spike Snowpipe Streaming et miroir (décision)

**Steps:**
1. Documentation à jour via Context7 : SDK Snowpipe Streaming (Java et Python,
   architecture haute performance), coût, garanties d’ordre et de doublons,
   offset tokens ; tâches déclenchées (intervalle minimal) ; tables dynamiques
   (`TARGET_LAG` minimal).
2. Prototype hors dépôt : pousser les événements d’un run de qualification dans
   une table d’historique par Snowpipe Streaming, avec l’identité d’événement
   comme clé d’idempotence et l’offset token relié au checkpoint.
3. Mesurer trois options de miroir : tâche déclenchée MERGE sur stream ;
   MERGE émis par le lecteur sur warehouse XS auto-suspendu ; table dynamique.
   Pour chacune : latence p50/p95 écriture IBM i → miroir, crédits par heure
   active et au repos, comportement sur rejeu et suppression.
4. Écrire `docs/decisions/2026-xx-miroir-snowflake.md` : option retenue, chiffres,
   limites, impact sur le design (section 5).

### Task 1.7 : requalification du chantier 1

**Steps:**
1. Reconstruire l’image de capture, relancer le harnais privé complet (copie,
   changements, arrêt/reprise, rejeu, rotation, rapprochement trois voies) sur
   GCS et sur S3 (compte AWS non client à fournir).
2. Ajouter au harnais la mesure de latence par écriture (horodatage source →
   objet brut → historique Snowflake).
3. Consigner résultats et écarts dans le rapport privé ; ouvrir le plan détaillé
   du chantier 2 avec les décisions 1.3, 1.5 et 1.6.

**Sortie du chantier 1 :** suite complète verte, rapprochement PASS sur les deux
stockages, latence mesurée, trois décisions écrites.
