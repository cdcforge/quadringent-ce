"""Rendu du rapport de run : JSON (complet) et résumé Markdown en français."""

from __future__ import annotations

import json

from .orchestrator import RunReport

_STATUS_LABEL = {"PASS": "réussie", "FAIL": "échouée", "SKIPPED": "ignorée"}


def to_json(report: RunReport) -> str:
    """Rapport complet, sérialisé JSON (une différence par ligne, rien de résumé)."""
    return json.dumps(report.as_dict(), indent=2, ensure_ascii=False, default=str) + "\n"


def _step_line(step: dict[str, object]) -> str:
    label = _STATUS_LABEL.get(str(step["status"]), str(step["status"]))
    return f"| {step['name']} | {step['status']} ({label}) | {json.dumps(step['details'], ensure_ascii=False)} |"


def to_markdown(report: RunReport) -> str:
    """Résumé Markdown à partir d'un ``RunReport`` (voir :func:`render_markdown`)."""
    return render_markdown(report.as_dict())


def render_markdown(data: dict[str, object]) -> str:
    """Résumé PASS/FAIL par étape, comptes et latence, en français.

    Prend directement la forme dict de ``RunReport.as_dict()`` (ou son
    équivalent relu depuis un fichier JSON déjà produit) : c'est ce qui
    permet à ``qualification report`` de reconstruire le Markdown à partir
    d'un rapport JSON existant, sans dépendre des adaptateurs d'un run.

    Les emplacements de coût sont marqués explicitement absents tant
    qu'aucune mesure de coût n'a été fournie — jamais un faux zéro.
    """
    lines = [
        f"# Rapport de qualification — {data['run_id']}",
        "",
        f"**Statut global : {data['status']} ({_STATUS_LABEL.get(data['status'], data['status'])})**",
        "",
        "Provenance : " + {
            "offline_fake": "simulé — adaptateurs en mémoire ; aucune qualification externe.",
            "real": "adaptateurs réels — le verdict porte sur les étapes exécutées.",
        }.get(data.get("execution_mode"), "absente — mode d'exécution non renseigné."),
        "",
        "## Étapes",
        "",
        "| Étape | Statut | Détails |",
        "|---|---|---|",
    ]
    lines += [_step_line(s) for s in data["steps"]]
    lines.append("")

    lines.append("## Rapprochement trois voies")
    lines.append("")
    reconciliation = data.get("reconciliation")
    if reconciliation is None:
        lines.append("Étape `reconcile` non exécutée dans ce run.")
    else:
        counts = reconciliation["counts"]
        continuity = reconciliation["journal_continuity"]
        boundary = reconciliation["boundary"]
        positions_available = "missing_positions" in continuity
        missing = continuity["missing_positions"] if positions_available else continuity["missing_sequences"]
        unexpected = continuity["unexpected_positions"] if positions_available else continuity["unexpected_sequences"]
        duplicates = continuity["duplicate_positions"] if positions_available else continuity["duplicate_sequences"]
        unit = "position(s)" if positions_available else "séquence(s)"
        bootstrap = (
            f"{boundary['bootstrap_receiver']}/{boundary['bootstrap_sequence']}"
            if boundary.get("bootstrap_receiver") else str(boundary["bootstrap_sequence"])
        )
        lines += [
            f"Statut : **{reconciliation['status']}**.",
            "",
            f"- Clés oracle : {counts['oracle_keys']} ; source : {counts['source_keys']} ; "
            f"destination : {counts['destination_keys']}",
            f"- Évènements snapshot : {counts['snapshot_events']} ; journal : {counts['journal_events']}",
            f"- Continuité de journal : {'OK' if continuity['equal'] else 'ÉCART'} "
            f"({continuity['captured_events']} évènements capturés, "
            f"{len(missing)} {unit} manquante(s), "
            f"{len(unexpected)} inattendue(s), "
            f"{duplicates} doublon(s))",
            f"- Frontière (bootstrap {bootstrap}) : "
            f"{'OK' if boundary['equal'] else 'ÉCART'} "
            f"({boundary['journal_events_before_bootstrap']} évènement(s) avant la frontière)",
            f"- Images « avant » incohérentes : {len(reconciliation['before_image_mismatches'])}",
            f"- Rejeu divergent : {'OK' if reconciliation['replay']['equal'] else 'ÉCART'} "
            f"({reconciliation['replay']['replayed_event_ids_divergent']} occurrence(s) divergente(s))",
            f"- Relectures brutes : {reconciliation['replay']['raw_attempt_rows']} occurrence(s), "
            f"dont {reconciliation['replay']['replayed_event_ids_identical']} identique(s)",
            f"- Suppressions attendues bien absentes : {'oui' if reconciliation['deleted_keys_absent'] else 'non'}",
            "",
        ]
        if not positions_available:
            lines.append("Oracle historique par séquence seule : la rotation des receivers n'est pas certifiée.")
            lines.append("")
        for name, label in (
            ("oracle_vs_destination", "Oracle vs destination"),
            ("oracle_vs_source", "Oracle vs source"),
            ("source_vs_destination", "Source vs destination"),
        ):
            d = reconciliation[name]
            status = "égal" if d["equal"] else "ÉCART"
            lines.append(
                f"- {label} : {status} — {d['compared_keys']} clé(s) comparée(s), "
                f"{len(d['missing_keys'])} manquante(s), {len(d['extra_keys'])} excédentaire(s), "
                f"{len(d['value_differences'])} différence(s) de valeur"
            )
        lines.append("")
        history = reconciliation.get("history")
        if history is None:
            lines.append("Historique Snowflake : absent — unicité des EVENT_ID non vérifiée dans ce rapport.")
        else:
            lines.append(f"- Historique Snowflake : {history['rows']} ligne(s) physique(s), "
                         f"{history['distinct_event_ids']} EVENT_ID distinct(s), "
                         f"{history['duplicate_rows']} doublon(s), snapshots compris.")
        lines.append("")
        mirror = reconciliation.get("mirror")
        if mirror is None:
            lines.append("Miroir Snowflake : absent — non vérifié dans ce rapport.")
        else:
            lines.append(f"- Miroir Snowflake : {len(mirror['duplicate_keys'])} clé(s) dupliquée(s)")
            for name, label in (("oracle_vs_mirror", "Oracle vs miroir"),
                                ("destination_vs_mirror", "Historique matérialisé vs miroir")):
                d = mirror[name]
                lines.append(f"- {label} : {'égal' if d['equal'] else 'ÉCART'} — "
                             f"{len(d['missing_keys'])} manquante(s), {len(d['extra_keys'])} excédentaire(s), "
                             f"{len(d['value_differences'])} différence(s) de valeur")
        lines.append("")

    lines.append("## Latence")
    lines.append("")
    freshness = data.get("freshness")
    if freshness is None or freshness.get("count", 0) == 0:
        lines.append("Aucune mesure de fraîcheur pour ce run.")
    else:
        lines.append(f"- p50 : {freshness['p50']} s ; p95 : {freshness['p95']} s ; max : {freshness['max']} s "
                      f"(n={freshness['count']})")
    lines.append("")

    lines.append("## Coûts")
    lines.append("")
    lines.append("Absent — non mesuré pour ce run. Ne pas interpréter comme un coût nul.")
    lines.append("")
    return "\n".join(lines)
