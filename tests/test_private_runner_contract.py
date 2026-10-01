"""Admission des jobs privés avant toute allocation de runner hébergé."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import unittest
from typing import Any
import yaml

REPOSITORY = Path(__file__).resolve().parents[1]
ROOT = Path(
    os.environ.get("PRIVATE_RUNNER_WORKFLOWS", REPOSITORY / ".github" / "workflows")
)
TOKEN = re.compile(r"'(?:[^']*)'|[A-Za-z_][A-Za-z0-9_.]*|&&|\|\||!=|==|[!(),]")


def evaluate(expression: str, context: dict[str, Any], success: bool = True) -> Any:
    raw = str(expression).removeprefix("${{").removesuffix("}}").strip()
    # Préserver les valeurs citées : les labels JSON contiennent aussi de la ponctuation.
    position = 0
    transformed = []
    for match in TOKEN.finditer(raw):
        assert not raw[position : match.start()].strip(), raw[position : match.start()]
        token = match.group()
        position = match.end()
        if token.startswith("'"):
            transformed.append(token)
        elif token in ("&&", "||", "!"):
            transformed.append({"&&": "and", "||": "or", "!": "not"}[token])
        elif token in ("true", "false"):
            transformed.append(token.title())
        elif token in ("success", "format", "fromJSON", "(", ")", ",", "==", "!="):
            transformed.append(token)
        else:
            transformed.append("context.get(" + repr(token) + ', "")')
    assert not raw[position:].strip()
    return eval(
        " ".join(transformed),
        {"__builtins__": {}},
        {
            "context": context,
            "success": lambda: success,
            "format": lambda template, *args: template.format(*args),
            "fromJSON": json.loads,
        },
    )


class PrivateRunnerContract(unittest.TestCase):
    def context(self, release: bool = False) -> dict[str, Any]:
        repo = "example/quadringent"
        tag = "v0.2.3"
        return {
            "github.event.repository.private": True,
            "github.event_name": "workflow_dispatch" if release else "push",
            "github.ref": "refs/tags/" + tag if release else "refs/heads/main",
            "github.workflow_ref": repo
            + "/.github/workflows/"
            + ("release.yml@refs/tags/" + tag if release else "ci.yml@refs/heads/main"),
            "github.repository": repo,
            "github.sha": "a" * 40,
            "inputs.version": tag,
            "vars.PRIVATE_APPROVED_SHA": "a" * 40,
            "vars.PRIVATE_APPROVED_TAG": tag,
            "vars.PRIVATE_RUNNER_LABELS": '["self-hosted","Linux","ARM64","quadringent-private"]',
            "vars.PUBLICATION_APPROVED": "true",
        }

    def test_matrix_evaluates_actual_yaml_for_every_job(self) -> None:
        for name in ("ci.yml", "release.yml"):
            jobs = yaml.safe_load((ROOT / name).read_text())["jobs"]
            release = name == "release.yml"
            self.assertEqual(len(jobs), 4 if release else 7)
            negative = [
                (
                    "PR",
                    {
                        "github.event_name": "pull_request",
                        "github.ref": "refs/pull/1/merge",
                    },
                ),
                ("other event", {"github.event_name": "workflow_call"}),
                ("other ref", {"github.ref": "refs/heads/unreviewed"}),
                (
                    "other workflow",
                    {
                        "github.workflow_ref": "example/quadringent/.github/workflows/other.yml@refs/heads/main"
                    },
                ),
                ("other SHA", {"github.sha": "b" * 40}),
                ("missing approved SHA", {"vars.PRIVATE_APPROVED_SHA": ""}),
            ]
            if release:
                negative += [
                    ("wrong input", {"inputs.version": "v0.2.4"}),
                    ("wrong approved tag", {"vars.PRIVATE_APPROVED_TAG": "v0.2.4"}),
                ]
            for job_name, job in jobs.items():
                context = self.context(release)
                with self.subTest(workflow=name, job=job_name, case="approved"):
                    self.assertTrue(evaluate(job["if"], context))
                    self.assertIn("self-hosted", evaluate(job["runs-on"], context))
                for case, delta in negative:
                    with self.subTest(workflow=name, job=job_name, case=case):
                        self.assertFalse(evaluate(job["if"], {**context, **delta}))
                        for fallback in (
                            {"github.event.repository.private": False},
                            {"vars.PRIVATE_RUNNER_LABELS": ""},
                        ):
                            default = {**context, **delta, **fallback}
                            self.assertTrue(evaluate(job["if"], default))
                            self.assertEqual(
                                evaluate(job["runs-on"], default), ["ubuntu-latest"]
                            )
                for case, delta in [
                    ("public", {"github.event.repository.private": False}),
                    ("private default", {"vars.PRIVATE_RUNNER_LABELS": ""}),
                ]:
                    with self.subTest(workflow=name, job=job_name, case=case):
                        self.assertTrue(evaluate(job["if"], {**context, **delta}))
                        self.assertEqual(
                            evaluate(job["runs-on"], {**context, **delta}),
                            ["ubuntu-latest"],
                        )
                self.assertFalse(
                    evaluate(job["if"], context, success=False),
                    "Des dépendances échouées ou ignorées doivent empêcher toute allocation",
                )
                if release and job_name in ("images", "chart"):
                    self.assertFalse(
                        evaluate(
                            job["if"], {**context, "vars.PUBLICATION_APPROVED": "false"}
                        )
                    )

    def test_canonical_dependencies_and_release_proofs_remain(self) -> None:
        ci = yaml.safe_load((ROOT / "ci.yml").read_text())["jobs"]
        rel = yaml.safe_load((ROOT / "release.yml").read_text())["jobs"]
        self.assertEqual(
            ci["quadringent-product-gate"]["needs"],
            ["image-runtime", "verifier-runtime", "cockpit-runtime"],
        )
        self.assertEqual(rel["images"]["needs"], ["validate"])
        self.assertEqual(rel["chart"]["needs"], ["validate"])
        self.assertEqual(rel["release"]["needs"], ["images", "chart", "validate"])
        for name in ("images", "chart", "release"):
            self.assertEqual(rel[name]["environment"], "publication-approved")
        self.assertEqual(rel["images"]["permissions"]["id-token"], "write")
        validate = "\n".join(s.get("run", "") for s in rel["validate"]["steps"])
        for proof in (
            "check_release_ci.py",
            '--sha "$GITHUB_SHA"',
            "refs/tags/$RELEASE_TAG",
            "PRIVATE_RUNNER_LABELS",
            "git fetch --no-tags origin refs/heads/main",
            "git rev-parse FETCH_HEAD",
            "sync_version.py --check --release",
        ):
            self.assertIn(proof, validate)
        self.assertFalse(
            any(j.get("uses", "").endswith("ci.yml") for j in rel.values())
        )


if __name__ == "__main__":
    unittest.main()
