from pathlib import Path
from contextlib import nullcontext
import json
import tempfile
from unittest.mock import patch

import pytest
import yaml

from quadringent.installer.vm_access import (
    VmAccessError, _fetch_gcp_kubeconfig, _fetch_kubeconfig, _write_local_kubeconfig, connect_gcp_vm,
)
from quadringent.installer.runner import CommandResult, RecordingRunner
from quadringent.installer.runner import SubprocessRunner


def test_ssm_kubeconfig_is_private_and_points_at_local_tunnel() -> None:
    source = yaml.safe_dump({
        "clusters": [{"cluster": {"server": "https://127.0.0.1:6443", "certificate-authority-data": "test"}, "name": "local"}],
        "users": [{"name": "local", "user": {"client-key-data": "private-test-key"}}],
        "contexts": [],
    })
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        _write_local_kubeconfig(source, path, 16443)
        loaded = yaml.safe_load(path.read_text())
        assert loaded["clusters"][0]["cluster"]["server"] == "https://127.0.0.1:16443"
        assert loaded["users"][0]["user"]["client-key-data"] == "private-test-key"
        assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_ssm_kubeconfig_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(VmAccessError, match="invalide"):
            _write_local_kubeconfig("not: a kubeconfig", Path(tmp) / "config.yaml", 16443)


def test_vm_private_files_refuse_symlinks() -> None:
    source = yaml.safe_dump({
        "clusters": [{"cluster": {"server": "https://127.0.0.1:6443"}, "name": "local"}],
        "users": [{"name": "local", "user": {"client-key-data": "private-test-key"}}],
    })
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.write_text("unchanged")
        link = Path(tmp) / "k3s-kubeconfig.yaml"
        link.symlink_to(target)
        with pytest.raises(VmAccessError, match="privé"):
            _write_local_kubeconfig(source, link, 16443)
        assert target.read_text() == "unchanged"
        with pytest.raises(OSError):
            SubprocessRunner().start(("echo", "never-started"), stdout_path=str(link))
        assert target.read_text() == "unchanged"


def test_kubeconfig_fetch_waits_for_cloud_init() -> None:
    command_id = "01234567-89ab-cdef-0123-456789abcdef"
    runner = RecordingRunner(scripted_results={
        ("aws", "ssm", "send-command", "--instance-ids", "i-0123456789abcdef0",
         "--document-name", "AWS-RunShellScript",
         "--parameters", json.dumps({"commands": ["sudo cloud-init status --wait >/dev/null && sudo cat /etc/rancher/k3s/k3s.yaml"]}),
         "--output", "json"): CommandResult(("aws", "ssm", "send-command"), 0, json.dumps({"Command": {"CommandId": command_id}})),
        ("aws", "ssm", "get-command-invocation", "--command-id", command_id,
         "--instance-id", "i-0123456789abcdef0", "--output", "json"): CommandResult(
             ("aws", "ssm", "get-command-invocation"), 0,
             json.dumps({"Status": "Success", "StandardOutputContent": "clusters: []"})),
    })
    assert _fetch_kubeconfig("i-0123456789abcdef0", runner, {"AWS_PROFILE": "aws-test"}) == "clusters: []"


def test_gcp_kubeconfig_is_read_through_ssh_iap_without_logging_its_key() -> None:
    command = (
        "gcloud", "compute", "ssh", "demo-quadringent-vm", "--project=example-gcp-project",
        "--zone=europe-west1-b", "--tunnel-through-iap", "--quiet",
        "--ssh-flag=-oBatchMode=yes", "--ssh-flag=-oConnectTimeout=10",
        "--command=sudo systemctl is-active --quiet k3s && sudo cat /etc/rancher/k3s/k3s.yaml",
    )
    runner = RecordingRunner(scripted_results={
        command: CommandResult(command, 0, "clusters: []\nusers: []\nprivate-test-key: hidden\n"),
    })
    result = _fetch_gcp_kubeconfig(
        "demo-quadringent-vm", "europe-west1-b", "example-gcp-project", runner, {},
    )
    assert "private-test-key" in result
    assert runner.calls[0].argv == command


def test_gcp_vm_tunnel_uses_ssh_over_iap_and_is_closed() -> None:
    source = yaml.safe_dump({
        "clusters": [{"cluster": {"server": "https://127.0.0.1:6443", "certificate-authority-data": "test"}, "name": "local"}],
        "users": [{"name": "local", "user": {"client-key-data": "private-test-key"}}],
        "contexts": [],
    })

    class FakeProcess:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            raise AssertionError("terminaison douce attendue")

    class FakeRunner(RecordingRunner):
        def __init__(self):
            super().__init__()
            self.process = FakeProcess()
            self.started = None

        def start(self, argv, *, env=None, stdout_path):
            self.started = (tuple(argv), stdout_path)
            return self.process

    runner = FakeRunner()
    with tempfile.TemporaryDirectory() as tmp, \
            patch("quadringent.installer.vm_access._fetch_gcp_kubeconfig", return_value=source), \
            patch("quadringent.installer.vm_access._available_port", return_value=16443), \
            patch("quadringent.installer.vm_access.socket.create_connection", return_value=nullcontext()):
        with connect_gcp_vm(
            "demo-quadringent-vm", "europe-west1-b", "example-gcp-project", Path(tmp), runner, {},
        ) as env:
            assert env["KUBECONFIG"] == str(Path(tmp) / "k3s-kubeconfig.yaml")
            assert Path(env["KUBECONFIG"]).stat().st_mode & 0o777 == 0o600
        assert runner.process.terminated
        assert runner.started[0][:4] == ("gcloud", "compute", "ssh", "demo-quadringent-vm")
        assert "--tunnel-through-iap" in runner.started[0]
        assert "-N" in runner.started[0]
        assert runner.started[1].endswith("iap-port-forward.log")
