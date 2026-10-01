#!/usr/bin/env python3
"""Run the reproducible offline acceptance checks for the ingestion POC.

This command deliberately does not connect to IBM i, AWS, Snowflake or
Popsink.  It reports local evidence separately from runtime gates that still
need an approved DEV runner.  Command output is never copied to the JSON
report, which keeps the guard safe to run in CI logs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


_TEST_COUNT_RE = re.compile(r"\bRan\s+(\d+)\s+tests?\b")
_PYTEST_COUNT_RE = re.compile(r"(?:^|\s)(\d+)\s+passed\b")
_VALID_STATUSES = frozenset({"PASS", "FAIL", "UNAVAILABLE", "UNVERIFIED"})


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    duration_ms: int | None = None
    exit_code: int | None = None
    tests_run: int | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"unsupported check status: {self.status}")

    def as_record(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def parse_unit_test_count(output: str) -> int | None:
    """Extract unittest or pytest's summary count without retaining output."""

    match = _TEST_COUNT_RE.search(output)
    if match is None:
        match = _PYTEST_COUNT_RE.search(output)
    return int(match.group(1)) if match else None


def python_test_command(root: Path) -> list[str]:
    """Return the pytest arguments for the current repository layout."""

    if not (root / "tests").is_dir():
        raise ValueError("tests directory is missing")
    return ["-m", "pytest", "-q"]


def fault_matrix_command(root: Path) -> list[str]:
    """Return the fault-matrix script path for the current repository layout."""

    relative = Path("scripts/raw_checkpoint_fault_matrix.py")
    if not (root / relative).is_file():
        raise ValueError("fault matrix script is missing")
    return [relative.as_posix()]


def parse_json_status(output: str) -> str | None:
    """Read only a top-level status field from a JSON command result."""

    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        return None
    return payload["status"]


def completion_status(checks: Sequence[CheckResult]) -> str:
    """Return COMPLETE only when every declared gate has a positive proof."""

    return "COMPLETE" if checks and all(check.status == "PASS" for check in checks) else "INCOMPLETE"


def local_checks_status(checks: Sequence[CheckResult]) -> str:
    """Return PASS only when core checks and secret scanning are proven."""

    mandatory = {"python_tests", "fault_matrix", "diff_check"}
    mandatory_checks = [check for check in checks if check.name in mandatory]
    if not mandatory_checks or not all(check.status == "PASS" for check in mandatory_checks):
        return "FAIL"

    secret_checks = [check for check in checks if check.name == "secret_scan"]
    if not secret_checks or secret_checks[0].status in {"UNAVAILABLE", "UNVERIFIED"}:
        return "UNVERIFIED"
    return "PASS" if secret_checks[0].status == "PASS" else "FAIL"


def _tool_available(command: str) -> bool:
    return bool(shutil.which(command))


def pinned_maven_image(root: Path) -> str:
    """Return the digest-pinned Maven image used by the capture Dockerfile."""

    dockerfile = root / "docker" / "Dockerfile"
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("FROM maven:"):
            return stripped.split()[1]
    raise ValueError("pinned maven image not found in continuous.Dockerfile")


def docker_maven_package_args(root: Path) -> list[str]:
    """Build the current Java sources with the pinned Maven image."""

    maven_home = Path.home() / ".m2"
    args = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "MAVEN_CONFIG=/var/maven/.m2",
        "-e",
        "HOME=/var/maven",
        "-v",
        f"{root.resolve()}:/workspace",
        "-w",
        "/workspace",
    ]
    if maven_home.is_dir():
        args.extend(["-v", f"{maven_home}:/var/maven/.m2"])
    args.extend(
        [
            pinned_maven_image(root),
            "mvn",
            "-f",
            "java/pom.xml",
            "-DskipTests",
            "package",
        ]
    )
    return args


def select_java_build_command(
    root: Path,
    *,
    mvn_available: bool,
    java_runtime_available: bool,
    docker_available: bool,
) -> list[str] | None:
    """Prefer a local JDK, then the pinned Docker Maven toolchain."""

    if mvn_available and java_runtime_available:
        return ["mvn", "-f", "java/pom.xml", "-DskipTests", "package"]
    if docker_available:
        return docker_maven_package_args(root)
    return None


def _java_runtime_available() -> bool:
    if not _tool_available("java"):
        return False
    try:
        completed = subprocess.run(
            ["java", "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _run_check(
    name: str,
    args: Sequence[str],
    *,
    root: Path,
    env: dict[str, str],
    timeout_seconds: int,
    parse_tests: bool = False,
    expected_json_status: str | None = None,
) -> CheckResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(args),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return CheckResult(
            name=name,
            status="UNAVAILABLE",
            duration_ms=_elapsed_ms(started),
            detail=f"executable not found: {args[0]}",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=name,
            status="FAIL",
            duration_ms=_elapsed_ms(started),
            detail=f"timeout after {timeout_seconds}s",
        )

    tests_run = parse_unit_test_count(completed.stdout + completed.stderr) if parse_tests else None
    status = "PASS" if completed.returncode == 0 else "FAIL"
    detail = None if status == "PASS" else f"command exited with code {completed.returncode}"
    if status == "PASS" and parse_tests and (tests_run is None or tests_run < 1):
        status = "FAIL"
        detail = "unittest summary missing or empty"
    if status == "PASS" and expected_json_status is not None:
        if parse_json_status(completed.stdout) != expected_json_status:
            status = "FAIL"
            detail = f"JSON status is not {expected_json_status}"
    return CheckResult(
        name=name,
        status=status,
        duration_ms=_elapsed_ms(started),
        exit_code=completed.returncode,
        tests_run=tests_run,
        detail=detail,
    )


def _unverified_runtime_gates() -> list[CheckResult]:
    return [
        CheckResult(
            name="ibmi_cntr_cud",
            status="UNVERIFIED",
            detail="requires approved DEV IBM i *BOTH window and business key",
        ),
        CheckResult(
            name="popsink_isochronous_benchmark",
            status="UNVERIFIED",
            detail="requires the same receiver/sequence and event fingerprints",
        ),
        CheckResult(
            name="soak_72h_and_cost",
            status="UNVERIFIED",
            detail="requires runtime metrics and attributed Snowflake/AWS cost",
        ),
    ]


def run_acceptance(root: Path, *, timeout_seconds: int = 180) -> dict[str, object]:
    root = root.resolve()
    environment = os.environ.copy()
    import_paths = (root, root / "src", root / "scripts", root / "tests")
    environment["PYTHONPATH"] = os.pathsep.join(
        str(path)
        for path in (*import_paths, environment.get("PYTHONPATH"))
        if path
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    checks = [
        _run_check(
            "python_tests",
            [sys.executable, *python_test_command(root)],
            root=root,
            env=environment,
            timeout_seconds=timeout_seconds,
            parse_tests=True,
        ),
        _run_check(
            "fault_matrix",
            [sys.executable, *fault_matrix_command(root)],
            root=root,
            env=environment,
            timeout_seconds=timeout_seconds,
            expected_json_status="PASS",
        ),
        _run_check(
            "diff_check",
            ["git", "diff", "--check"],
            root=root,
            env=environment,
            timeout_seconds=timeout_seconds,
        ),
    ]

    if _tool_available("gitleaks"):
        checks.append(
            _run_check(
                "secret_scan",
                ["gitleaks", "dir", ".", "--no-banner", "--no-color", "--redact", "--exit-code", "1"],
                root=root,
                env=environment,
                timeout_seconds=timeout_seconds,
            )
        )
    else:
        checks.append(CheckResult(name="secret_scan", status="UNAVAILABLE", detail="gitleaks not installed"))

    java_command = select_java_build_command(
        root,
        mvn_available=_tool_available("mvn"),
        java_runtime_available=_java_runtime_available(),
        docker_available=_tool_available("docker"),
    )
    if java_command is None:
        missing = [
            tool
            for tool, present in (
                ("mvn", _tool_available("mvn")),
                ("java", _java_runtime_available()),
                ("docker", _tool_available("docker")),
            )
            if not present
        ]
        checks.append(
            CheckResult(
                name="java_build",
                status="UNAVAILABLE",
                detail=f"missing toolchain: {', '.join(missing)}",
            )
        )
    else:
        checks.append(
            _run_check(
                "java_build",
                java_command,
                root=root,
                env=environment,
                timeout_seconds=timeout_seconds,
            )
        )

    runtime_gates = _unverified_runtime_gates()
    all_checks = checks + runtime_gates
    return {
        "scope": "offline-local",
        "local_checks_status": local_checks_status(checks),
        "completion_status": completion_status(all_checks),
        "checks": [check.as_record() for check in all_checks],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="POC repository root (defaults to the current worktree)",
    )
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    report = run_acceptance(args.repo_root, timeout_seconds=args.timeout_seconds)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["local_checks_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
