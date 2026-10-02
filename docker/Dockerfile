# Image de capture Quadringent. Dérivée du Dockerfile éprouvé de la phase de
# recherche, adaptée à la structure de ce dépôt.
#
# Bases épinglées par digest : une image de production ne dépend jamais d'un tag
# mouvant, et la chart refuse de se rendre sans digest pour la même raison.

FROM maven:3.9-eclipse-temurin-21@sha256:c07f7ccfb8ca6c9fa29ee523f00afa7d2ca6132c92f8652c4aebb5ee3491f502 AS build

WORKDIR /workspace
COPY java/pom.xml java/pom.xml
COPY java/third-party-sources.sha256 java/third-party-sources.sha256
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY licenses/ licenses/
COPY java/src java/src
RUN mvn -q -f java/pom.xml -DskipTests package dependency:copy-dependencies \
    -DincludeScope=runtime -DoutputDirectory=target/dependency \
    && mvn -q -f java/pom.xml dependency:copy-dependencies \
       -DincludeScope=runtime -Dclassifier=sources -Dmdep.failOnMissingClassifierArtifact=true \
       -DoutputDirectory=target/dependency-sources \
    && cd java/target/dependency-sources \
    && sha256sum -c ../../third-party-sources.sha256 \
    && test "$(find . -name '*-sources.jar' | wc -l)" -eq "$(wc -l < ../../third-party-sources.sha256)"

# JRE seul : aucun compilateur dans l'image finale.
FROM eclipse-temurin:21-jre-jammy@sha256:eebd356ad7358b7094758e5787a6726f332917cfd56feab6457c56dab895cdbf AS jre
FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f
COPY --from=jre /opt/java/openjdk /opt/java/openjdk

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH=/opt/java/openjdk/bin:$PATH \
    AS400_JAVA=java \
    AS400_JAVA_CLASSPATH=/app/probe.jar:/app/lib/* \
    AS400_JAVA_WORKER_CLASS=io.quadringent.as400.PersistentJournalWorker
# AS400_TLS_CA_FILE n'a plus de valeur par défaut ici (constat du 24
# septembre 2026 : cette image ne livre aucun fichier à ce chemin, donc
# AS400_TLS_CA_FILE=/app/certs/ibmi-ca.pem faisait planter toute sonde
# contre un IBM i à autorité publique). Sans cette variable, la sonde
# Python (``quadringent_source_probe_job.py::tls_probe``) et ``TlsTrust.
# java`` utilisent tous deux le magasin de confiance système/JVM par
# défaut — suffisant pour une autorité publique reconnue (ex. Sectigo).
# Une autorité privée/inconnue est mesurée (jamais authentifiée) et
# épinglée par l'opérateur ; c'est alors le control plane qui pose cette
# variable (Secret/ConfigMap monté) sur les charges qui en ont besoin —
# jamais un défaut d'image.

RUN apt-get update \
    && apt-get upgrade --yes \
    && apt-get install --no-install-recommends --yes ca-certificates libpcre2-8-0 tini \
    && test -s /usr/share/doc/tini/copyright \
    && python -m venv /opt/venv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/quadringent/
COPY licenses/ /usr/share/quadringent/licenses/
COPY scripts/collect_python_notices.py /tmp/collect_python_notices.py
RUN /opt/venv/bin/python -m pip uninstall --yes pip \
    && python -m pip uninstall --yes pip
RUN /opt/venv/bin/python /tmp/collect_python_notices.py --output /usr/share/quadringent/python-licenses \
    && rm /tmp/collect_python_notices.py
COPY --from=build /workspace/java/target/as400-journal-reader.jar /app/probe.jar
COPY --from=build /workspace/java/target/dependency /app/lib
COPY --from=build /workspace/java/target/dependency-sources /usr/share/quadringent/sources
COPY java/third-party-sources.sha256 /usr/share/quadringent/
# Seuls les modules du chemin d'exécution entrent dans l'image. Les outils de
# mesure, de comparaison et de rejeu restent au dépôt : ils n'ont pas à être
# livrés chez un client, et chaque fichier en moins est une surface en moins.
# Liste dérivée d'une analyse de dépendances depuis le point d'entrée : voir
# docker/runtime-modules.txt.
COPY src/quadringent/__init__.py /app/quadringent/
COPY src/quadringent/checkpoint.py /app/quadringent/
COPY src/quadringent/console_snapshot.py /app/quadringent/
COPY src/quadringent/continuous.py /app/quadringent/
COPY src/quadringent/contract.py /app/quadringent/
COPY src/quadringent/destination_proof.py /app/quadringent/
COPY src/quadringent/destination_sync.py /app/quadringent/
COPY src/quadringent/fleet_capture.py /app/quadringent/
COPY src/quadringent/fleet_certify_probe.py /app/quadringent/
COPY src/quadringent/gcs_backend.py /app/quadringent/
COPY src/quadringent/ibmi_reader.py /app/quadringent/
COPY src/quadringent/java_catalog.py /app/quadringent/
COPY src/quadringent/java_worker.py /app/quadringent/
COPY src/quadringent/lag_history.py /app/quadringent/
COPY src/quadringent/object_store.py /app/quadringent/
COPY src/quadringent/observability_snapshot.py /app/quadringent/
COPY src/quadringent/proof_windows.py /app/quadringent/
COPY src/quadringent/raw.py /app/quadringent/
COPY src/quadringent/run_reservation.py /app/quadringent/
COPY src/quadringent/site_config.py /app/quadringent/
COPY src/quadringent/infrastructure_costs.py /app/quadringent/
COPY src/quadringent/sql_window.py /app/quadringent/
COPY src/quadringent/slo.py /app/quadringent/
COPY src/quadringent/slo_alerts.py /app/quadringent/
COPY src/quadringent/slo_telemetry.py /app/quadringent/
COPY src/quadringent/snowflake_loader.py /app/quadringent/
COPY src/quadringent/snowflake_replay.py /app/quadringent/
# Importés par le paquet quadringent_control_plane copié plus bas (sonde de
# source) ; le chargeur Snowflake lui-même tourne dans l'image du control
# plane, seule à porter les dépendances Snowflake.
COPY src/quadringent/snowflake_destination.py /app/quadringent/
COPY src/quadringent/snowflake_streaming_loader.py /app/quadringent/
COPY src/quadringent/source_gate.py /app/quadringent/
COPY src/quadringent/storage_backend.py /app/quadringent/
COPY src/quadringent/storage_layout.py /app/quadringent/
COPY src/quadringent_control_plane /app/quadringent_control_plane
COPY scripts/as400_checkpoint.py /app/
COPY scripts/as400_continuous_capture.py /app/
COPY scripts/as400_snapshot_publish.py /app/
COPY scripts/as400_source_gate.py /app/
COPY scripts/as400_sql_journal_capture.py /app/
COPY scripts/quadringent_control_plane.py /app/
# Job de copie initiale (chantier « prod-wiring », constat du 24 septembre
# 2026) : instantané JDBC (ReadOnlyTableSnapshot, déjà dans probe.jar) ->
# publication (as400_snapshot_publish.py, ci-dessus) -> preuve
# (v2/executor/evidence.py, jamais importé ici — cette image ne porte pas
# quadringent_control_plane, la preuve est réécrite au même format, testé
# côté dépôt par InitialCopyEvidence.from_dict).
COPY scripts/quadringent_initial_copy_job.py /app/
# Jobs de diagnostic (sonde de source, découverte de tables — chantier
# « prod-wiring ») : lancés par le control plane v2 (qui n'a pas Java) avec
# cette image, via un ``command`` explicite qui remplace l'ENTRYPOINT par
# défaut. ``quadringent_source_probe_job.py`` importe
# ``quadringent_control_plane.v2.services.source_probe`` (déjà copié
# ci-dessus avec tout le paquet ``quadringent_control_plane``) — logique de
# résultat partagée, jamais dupliquée.
COPY scripts/quadringent_source_probe_job.py /app/
COPY scripts/quadringent_table_discovery_job.py /app/

RUN useradd --create-home --uid 10001 capture && chown -R capture /app
USER 10001

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/venv/bin/python", "/app/as400_continuous_capture.py"]
