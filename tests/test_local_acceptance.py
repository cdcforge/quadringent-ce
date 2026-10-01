from __future__ import annotations

from pathlib import Path
import unittest

from scripts.local_acceptance import (
    CheckResult,
    completion_status,
    fault_matrix_command,
    local_checks_status,
    parse_unit_test_count,
    parse_json_status,
    python_test_command,
)


class LocalAcceptanceContractTests(unittest.TestCase):
    def test_parse_unit_test_count_from_unittest_output(self) -> None:
        output = "Ran 42 tests in 0.568s\n\nOK\n"

        self.assertEqual(parse_unit_test_count(output), 42)

    def test_parse_unit_test_count_returns_none_without_summary(self) -> None:
        self.assertIsNone(parse_unit_test_count("test output without summary"))

    def test_parse_unit_test_count_from_pytest_output(self) -> None:
        output = "636 passed, 1 skipped, 75 subtests passed in 70.55s\n"

        self.assertEqual(parse_unit_test_count(output), 636)

    def test_acceptance_commands_use_the_current_repository_layout(self) -> None:
        root = Path(__file__).resolve().parents[1]

        self.assertEqual(
            python_test_command(root),
            ["-m", "pytest", "-q"],
        )
        self.assertEqual(
            fault_matrix_command(root),
            ["scripts/raw_checkpoint_fault_matrix.py"],
        )

    def test_parse_json_status_reads_only_the_safe_status_field(self) -> None:
        self.assertEqual(parse_json_status('{"status": "PASS", "events": 2}'), "PASS")
        self.assertIsNone(parse_json_status("not json"))

    def test_completion_never_claims_complete_with_unverified_runtime_gates(self) -> None:
        checks = [
            CheckResult(name="python_tests", status="PASS"),
            CheckResult(name="java_build", status="UNAVAILABLE"),
            CheckResult(name="pays_cud", status="UNVERIFIED"),
        ]

        self.assertEqual(completion_status(checks), "INCOMPLETE")

    def test_completion_is_complete_only_when_every_check_passes(self) -> None:
        checks = [
            CheckResult(name="python_tests", status="PASS"),
            CheckResult(name="java_build", status="PASS"),
        ]

        self.assertEqual(completion_status(checks), "COMPLETE")

    def test_local_checks_pass_only_with_a_successful_secret_scan(self) -> None:
        checks = [
            CheckResult(name="python_tests", status="PASS"),
            CheckResult(name="fault_matrix", status="PASS"),
            CheckResult(name="diff_check", status="PASS"),
            CheckResult(name="secret_scan", status="PASS"),
        ]

        self.assertEqual(local_checks_status(checks), "PASS")

    def test_local_checks_fail_when_the_secret_scan_fails(self) -> None:
        checks = [
            CheckResult(name="python_tests", status="PASS"),
            CheckResult(name="fault_matrix", status="PASS"),
            CheckResult(name="diff_check", status="PASS"),
            CheckResult(name="secret_scan", status="FAIL"),
        ]

        self.assertEqual(local_checks_status(checks), "FAIL")

    def test_local_checks_are_unverified_when_secret_scan_is_unavailable(self) -> None:
        checks = [
            CheckResult(name="python_tests", status="PASS"),
            CheckResult(name="fault_matrix", status="PASS"),
            CheckResult(name="diff_check", status="PASS"),
            CheckResult(name="secret_scan", status="UNAVAILABLE"),
        ]

        self.assertEqual(local_checks_status(checks), "UNVERIFIED")

    def test_local_checks_are_unverified_when_secret_scan_is_not_proven(self) -> None:
        checks = [
            CheckResult(name="python_tests", status="PASS"),
            CheckResult(name="fault_matrix", status="PASS"),
            CheckResult(name="diff_check", status="PASS"),
            CheckResult(name="secret_scan", status="UNVERIFIED"),
        ]

        self.assertEqual(local_checks_status(checks), "UNVERIFIED")

    def test_pinned_maven_image_matches_continuous_dockerfile(self) -> None:
        from scripts.local_acceptance import pinned_maven_image

        root = Path(__file__).resolve().parents[1]
        image = pinned_maven_image(root)

        self.assertTrue(image.startswith("maven:3.9-eclipse-temurin-21@sha256:"))
        dockerfile = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(f"FROM {image}", dockerfile)

    def test_docker_maven_package_args_compile_current_source_without_tests(self) -> None:
        from scripts.local_acceptance import docker_maven_package_args, pinned_maven_image

        root = Path(__file__).resolve().parents[1]
        args = docker_maven_package_args(root)

        self.assertEqual(args[0], "docker")
        self.assertIn("run", args)
        self.assertIn("--rm", args)
        self.assertIn(pinned_maven_image(root), args)
        self.assertIn("java/pom.xml", " ".join(args))
        self.assertIn("-DskipTests", args)
        self.assertIn("package", args)

    def test_select_java_build_uses_docker_when_path_maven_is_missing(self) -> None:
        from scripts.local_acceptance import select_java_build_command

        root = Path(__file__).resolve().parents[1]
        command = select_java_build_command(
            root,
            mvn_available=False,
            java_runtime_available=False,
            docker_available=True,
        )

        self.assertIsNotNone(command)
        self.assertEqual(command[0], "docker")
        self.assertIn("package", command)

    def test_select_java_build_is_unavailable_without_maven_or_docker(self) -> None:
        from scripts.local_acceptance import select_java_build_command

        root = Path(__file__).resolve().parents[1]
        command = select_java_build_command(
            root,
            mvn_available=False,
            java_runtime_available=False,
            docker_available=False,
        )

        self.assertIsNone(command)


if __name__ == "__main__":
    unittest.main()
