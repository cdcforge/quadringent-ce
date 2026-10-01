# Dedicated verification runtime: no JVM, IBM i driver or embedded credentials.
FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f
RUN apt-get update \
    && apt-get upgrade --yes \
    && apt-get install --no-install-recommends --yes libpcre2-8-0 \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src
COPY docker/verifier-requirements.txt /tmp/requirements.txt
COPY scripts/strip_pyjwt_description.py /tmp/strip_pyjwt_description.py
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && python /tmp/strip_pyjwt_description.py \
    && rm /tmp/requirements.txt /tmp/strip_pyjwt_description.py \
    && useradd --uid 10001 --create-home verifier
WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/quadringent/
COPY licenses/ /usr/share/quadringent/licenses/
COPY scripts/collect_python_notices.py /tmp/collect_python_notices.py
RUN python -m pip uninstall --yes pip
RUN python /tmp/collect_python_notices.py --output /usr/share/quadringent/python-licenses \
    && rm /tmp/collect_python_notices.py
COPY src/quadringent /app/src/quadringent
COPY src/quadringent_control_plane /app/src/quadringent_control_plane
COPY scripts/quadringent_autonomous_verify.py /app/scripts/quadringent_autonomous_verify.py
COPY scripts/quadringent_window_verify.py /app/scripts/quadringent_window_verify.py
COPY scripts/quadringent_window_supervise.py /app/scripts/quadringent_window_supervise.py
COPY scripts/quadringent_window_chain_read.py /app/scripts/quadringent_window_chain_read.py
COPY scripts/quadringent_slo_collect.py /app/scripts/quadringent_slo_collect.py
COPY scripts/quadringent_observability_refresh.py /app/scripts/quadringent_observability_refresh.py
COPY scripts/quadringent_fleet_observe.py /app/scripts/quadringent_fleet_observe.py
COPY scripts/quadringent_slo_alerts.py /app/scripts/quadringent_slo_alerts.py
COPY scripts/quadringent_preflight.py /app/scripts/quadringent_preflight.py
USER 10001
ENTRYPOINT ["python", "/app/scripts/quadringent_autonomous_verify.py"]
