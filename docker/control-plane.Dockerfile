FROM node:24-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS ui-build
WORKDIR /build/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
COPY LICENSE NOTICE /build/
RUN npm run build

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

FROM eclipse-temurin:21-jre-jammy@sha256:eebd356ad7358b7094758e5787a6726f332917cfd56feab6457c56dab895cdbf AS jre

FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f
RUN apt-get update \
    && apt-get upgrade --yes \
    && apt-get install --no-install-recommends --yes libpcre2-8-0 \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/opt/java/openjdk/bin:$PATH \
    AS400_JAVA=java \
    AS400_JAVA_CLASSPATH=/app/probe.jar:/app/lib/*
COPY --from=jre /opt/java/openjdk /opt/java/openjdk
COPY docker/control-plane-requirements.txt /tmp/requirements.txt
COPY scripts/strip_pyjwt_description.py /tmp/strip_pyjwt_description.py
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && python /tmp/strip_pyjwt_description.py \
    && rm /tmp/requirements.txt /tmp/strip_pyjwt_description.py \
    && useradd --uid 10001 --create-home cockpit
WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/quadringent/
COPY licenses/ /usr/share/quadringent/licenses/
COPY scripts/collect_python_notices.py /tmp/collect_python_notices.py
RUN python -m pip uninstall --yes pip
RUN python /tmp/collect_python_notices.py --output /usr/share/quadringent/python-licenses \
    && rm /tmp/collect_python_notices.py
COPY --from=build /workspace/java/target/as400-journal-reader.jar /app/probe.jar
COPY --from=build /workspace/java/target/dependency /app/lib
COPY --from=build /workspace/java/target/dependency-sources /usr/share/quadringent/sources
COPY java/third-party-sources.sha256 /usr/share/quadringent/
COPY src/quadringent/__init__.py src/quadringent/site_config.py src/quadringent/slo.py src/quadringent/slo_alerts.py /app/src/quadringent/
COPY src/quadringent/infrastructure_costs.py src/quadringent/storage_backend.py src/quadringent/gcs_backend.py src/quadringent/checkpoint.py /app/src/quadringent/
COPY src/quadringent/proof_windows.py src/quadringent/contract.py src/quadringent/object_store.py src/quadringent/raw.py src/quadringent/observability_snapshot.py src/quadringent/destination_proof.py src/quadringent/destination_sync.py /app/src/quadringent/
COPY src/quadringent/continuous.py src/quadringent/ibmi_reader.py src/quadringent/java_catalog.py src/quadringent/java_worker.py src/quadringent/source_gate.py src/quadringent/sql_window.py src/quadringent/slo_telemetry.py src/quadringent/snowflake_loader.py src/quadringent/snowflake_replay.py /app/src/quadringent/
COPY src/quadringent/fleet_capture.py src/quadringent/fleet_certify_probe.py src/quadringent/table_discovery.py /app/src/quadringent/
COPY src/quadringent/snowflake_destination.py src/quadringent/snowflake_streaming_loader.py src/quadringent/snowflake_sql_loader.py src/quadringent/storage_layout.py /app/src/quadringent/
COPY src/quadringent_control_plane /app/src/quadringent_control_plane
COPY scripts/quadringent_control_plane.py /app/quadringent_control_plane.py
COPY scripts/quadringent_control_plane_v2.py /app/quadringent_control_plane_v2.py
COPY scripts/quadringent_postgres_backup.py /app/quadringent_postgres_backup.py
COPY scripts/quadringent_healthcheck.py /app/quadringent_healthcheck.py
COPY scripts/quadringent_preflight.py /app/scripts/quadringent_preflight.py
COPY scripts/quadringent_destination_loader.py /app/quadringent_destination_loader.py
COPY --from=ui-build /build/ui/dist /app/ui
USER 10001
ENTRYPOINT ["python", "/app/quadringent_control_plane.py", "--ui-dist", "/app/ui"]
