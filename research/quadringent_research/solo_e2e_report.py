"""Emit the dated solo-e2e markdown+JSON pair from frozen scratch proofs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Preuve historique : ce module émet le rapport daté d'une campagne passée.
# Les identifiants d'installation qu'il contient (digest d'image, mesures de
# référence) sont le contenu figé de cette preuve, pas la configuration du
# produit — ils ne doivent pas être paramétrés ni réutilisés par défaut.
REPORT_STEM = "2026-08-26_retrievejournal-solo-e2e"
SCRATCH_FILES = (
    "capture-metrics.json",
    "snowflake-e2e.json",
    "verdict.json",
    "popsink-paused.json",
    "popsink-restored.json",
)
VERDICT_VALUES = {"oui", "non", "INCOMPLETE"}
IMAGE = "sha256:b984c7d435bf5edb4c1a963d5b7816d7780196836a50f0afe8591d9bb3c84a1f"
PRIOR_CRUSH3_EVT = 29.112
PRIOR_R2E_EVT = 730.132


def load_scratch(scratch: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name in SCRATCH_FILES:
        path = scratch / name
        if not path.is_file():
            raise FileNotFoundError(name)
        payload[name] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def build_report(
    scratch: Path, *, database: str, schema: str
) -> dict[str, Any]:
    """Render the report; destination coordinates are declared by the caller."""

    if not isinstance(database, str) or not database.strip():
        raise ValueError("a declared destination database is required")
    if not isinstance(schema, str) or not schema.strip():
        raise ValueError("a declared destination schema is required")
    files = load_scratch(scratch)
    capture = files["capture-metrics.json"]
    snowflake = files["snowflake-e2e.json"]
    verdict_file = files["verdict.json"]
    paused = files["popsink-paused.json"]
    restored = files["popsink-restored.json"]

    verdict = verdict_file.get("verdict") or verdict_file.get(
        "tool_vs_popsink_cost_efficiency"
    )
    if verdict not in VERDICT_VALUES:
        raise ValueError("malformed verdict")

    crush = capture.get("crush") or {}
    best_eps = crush.get("best_events_per_sec")
    best_cpu = crush.get("best_cpu_ms_per_event")
    if not isinstance(best_eps, (int, float)) or not isinstance(best_cpu, (int, float)):
        raise ValueError("missing crush bars")

    volumes = _volumes(capture, snowflake)
    schema = _schema(snowflake, fallback=schema)
    pause_t0 = capture.get("pause_t0") or paused.get("t0")
    if not pause_t0:
        raise ValueError("missing pause_t0")

    ac2 = capture.get("ac2")
    if ac2 not in {"MET", "UNMET"}:
        raise ValueError("missing ac2")

    usd = verdict_file.get("usd") or crush.get("usd") or "INCOMPLETE"
    ibmi = restored.get("ibmi") or {}
    return {
        "ac2": ac2,
        "count": snowflake.get("count") or snowflake.get("ac2_same_window_table_count"),
        "database": database,
        "image": IMAGE,
        "mode": capture.get("mode"),
        "pause_t0": pause_t0,
        "paused": {
            "source_name": paused.get("source_name"),
            "status": paused.get("status"),
            "paused": paused.get("paused"),
            "stop_http": paused.get("stop_http"),
            "paused_wait_s": paused.get("paused_wait_s"),
        },
        "prior_evidence_not_this_run": {
            "crush3_live_events_per_sec": PRIOR_CRUSH3_EVT,
            "r2e_20260822_events_per_sec": PRIOR_R2E_EVT,
        },
        "restore": {
            "live": restored.get("live"),
            "ready": restored.get("ibmi_ready") or ibmi.get("ready"),
            "restarts": ibmi.get("restarts"),
            "suffix": ibmi.get("name_suffix"),
            "target_touched": restored.get("target_touched"),
            "useriddisabled_hits": restored.get("useriddisabled_hits"),
        },
        "schema": schema,
        "tables": snowflake.get("tables") or capture.get("landed_tables"),
        "usd": usd,
        "verdict": verdict,
        "verdict_bars": {
            "best_cpu_ms_per_event": best_cpu,
            "best_events_per_sec": best_eps,
            "cpu_bar": crush.get("bar_cpu_ms_per_event", 1.96),
            "cpu_pass": bool(best_cpu <= 1.96),
            "popsink_cpu_ms_per_event": 5.87,
            "popsink_events_per_sec": 103,
            "throughput_bar": crush.get("bar_events_per_sec", 515),
            "throughput_pass": bool(best_eps >= 515),
        },
        "volumes": volumes,
    }


def emit_report(
    scratch: Path, reports: Path, *, database: str, schema: str
) -> tuple[Path, Path]:
    report = build_report(scratch, database=database, schema=schema)
    reports.mkdir(parents=True, exist_ok=True)
    json_path = reports / f"{REPORT_STEM}.json"
    md_path = reports / f"{REPORT_STEM}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def gate_report_pair(json_path: Path, md_path: Path) -> dict[str, str]:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = md_path.read_text(encoding="utf-8")
    if payload.get("verdict") not in VERDICT_VALUES:
        return {"status": "GATE_FAIL", "reason": "malformed verdict"}
    if payload.get("ac2") == "UNMET" or payload.get("ac2") != "MET":
        return {"status": "GATE_FAIL", "reason": "ac2"}
    required = {
        "ac2": payload["ac2"],
        "pause_t0": payload["pause_t0"],
        "verdict": payload["verdict"],
        "usd": payload["usd"],
        "schema": payload["schema"],
    }
    for key, value in required.items():
        if str(value) not in markdown and f"**{value}**" not in markdown:
            return {"status": "GATE_FAIL", "reason": f"md_missing_{key}"}
    bars = payload.get("verdict_bars") or {}
    for key in ("best_events_per_sec", "best_cpu_ms_per_event"):
        if not _number_in_markdown(markdown, bars.get(key)):
            return {"status": "GATE_FAIL", "reason": f"md_missing_{key}"}
    for volume in payload.get("volumes") or []:
        if str(int(volume)) not in markdown and _fr(volume) not in markdown:
            return {"status": "GATE_FAIL", "reason": "md_missing_volume"}
    return {"status": "GATE_PASS", "reason": ""}


def render_markdown(report: dict[str, Any]) -> str:
    bars = report["verdict_bars"]
    paused = report["paused"]
    restore = report["restore"]
    volumes = ", ".join(str(item) for item in report["volumes"])
    tables = ", ".join(str(item) for item in (report.get("tables") or []))
    return f"""# Pause propre Popsink — contre-factuel DEV vs Popsink — 2026-08-26

Périmètre DEV. Dest `{report["database"]}.{report["schema"]}` only. Aucun secret.
Image `{report["image"]}`. FILE filter. Overlay hangfix : aucun.
Source : `{paused.get("source_name")}` (`POST stop` / `POST start` API officielle).
Target `snowflake-dev` : non touché. G8 interdit.

« Sources IBM i paused » = **côté Popsink** (status API `paused`). IBM i reste up.

## Verdict crush / coût-efficacité

**{report["verdict"]}** : cet outil n’est pas meilleur que Popsink **en runtime
continu DEV à sa place**.

| Barre | Popsink | Mesure pause (meilleur UNION p16) | Statut |
|---|---:|---:|---|
| ≥{_fr(bars["throughput_bar"])} evt/s | {_fr(bars["popsink_events_per_sec"])} | **{_fr(bars["best_events_per_sec"])}** | FAIL |
| ≤{_fr(bars["cpu_bar"])} CPU-ms/evt | {_fr(bars["popsink_cpu_ms_per_event"])} | **{_fr(bars["best_cpu_ms_per_event"])}** | FAIL |
| USD CDC isolé | — | {report["usd"]} | **{report["usd"]}** |

Preuves antérieures (pas ce run) : crush3 live {_fr(PRIOR_CRUSH3_EVT)} evt/s ;
r2e 22/08 {_fr(PRIOR_R2E_EVT)} evt/s sur un receveur disparu après rotation.

Pas de GO. Pas de G8. ac2={report["ac2"]} pause_t0={report["pause_t0"]}
schema={report["schema"]} usd={report["usd"]} verdict={report["verdict"]}

## AC2 « plusieurs tables at once »

**{report["ac2"]}.** Un retrieve JDBC UNION ALL object-filtered, 1 reader,
tables {tables}, volumes {volumes}, COPY/MERGE `{report["database"]}.{report["schema"]}`
count={report.get("count")} même pause_t0={report["pause_t0"]}.

## Pause / restore

stop_http={paused.get("stop_http")} status={paused.get("status")}
paused={paused.get("paused")} wait_s={paused.get("paused_wait_s")}
restore live={restore.get("live")} ready={restore.get("ready")}
restarts={restore.get("restarts")} suffix={restore.get("suffix")}
target_touched={restore.get("target_touched")}
useriddisabled_hits={restore.get("useriddisabled_hits")}
"""


def _volumes(capture: dict[str, Any], snowflake: dict[str, Any]) -> list[int]:
    batches: list[int] = []
    for item in capture.get("volumes") or []:
        batch = item.get("batch")
        if isinstance(batch, int):
            batches.append(batch)
    if len(batches) >= 2:
        return batches
    window = (snowflake.get("pause_window") or {}).get("volumes") or []
    for item in window:
        batch = item.get("batch_entries")
        if isinstance(batch, int):
            batches.append(batch)
    return batches


def _schema(snowflake: dict[str, Any], *, fallback: str) -> str:
    volumes = snowflake.get("volumes") or {}
    if isinstance(volumes, dict):
        for table_block in volumes.values():
            if not isinstance(table_block, dict):
                continue
            for proof in table_block.values():
                if not isinstance(proof, dict):
                    continue
                schema = (proof.get("metrics") or {}).get("schema")
                if schema:
                    return str(schema)
    return fallback


def _fr(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text.replace(".", ",")
    return str(value)


def _number_in_markdown(markdown: str, value: Any) -> bool:
    if value is None:
        return False
    plain = str(value)
    return plain in markdown or _fr(value) in markdown
