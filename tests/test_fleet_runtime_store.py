from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from quadringent_control_plane.fleet_runtime_store import (
    AtomicJsonStateStore,
    AtomicJsonStateStoreError,
)


def _store(tmp_path: Path) -> AtomicJsonStateStore:
    return AtomicJsonStateStore(tmp_path / "runtime-state.json")


def test_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = {
        "name": "fleet", "count": 2, "ratio": 1.5, "ok": True,
        "missing": None, "tags": ["a", "b"], "nested": {"k": "v"}, "unicode": "été",
    }
    store.save(payload)
    assert store.load() == payload


def test_absence_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.load() is None
    assert not (tmp_path / "runtime-state.json").exists()


def test_overwrite_is_atomic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save({"generation": 1, "label": "first"})
    store.save({"generation": 2, "label": "second"})
    assert store.load() == {"generation": 2, "label": "second"}
    assert [path.name for path in tmp_path.iterdir() if path.suffix == ".tmp"] == []


def test_concurrent_reads_and_writes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save({"n": 0})
    errors: list[BaseException] = []
    barrier = threading.Barrier(24)

    def writer(value: int) -> None:
        try:
            barrier.wait(timeout=5)
            store.save({"n": value})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            barrier.wait(timeout=5)
            loaded = store.load()
            assert isinstance(loaded, dict)
            assert isinstance(loaded["n"], int)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(12)]
    threads.extend(threading.Thread(target=reader) for _ in range(12))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert errors == []
    final = store.load()
    assert isinstance(final, dict)
    assert final["n"] in range(12)


def test_corruption_raises_redacted_error(tmp_path: Path) -> None:
    path = tmp_path / "runtime-state.json"
    secret = "super-secret-token-value"
    path.write_text(f'{{"token": "{secret}", invalid', encoding="utf-8")
    store = AtomicJsonStateStore(path)
    with pytest.raises(AtomicJsonStateStoreError) as captured:
        store.load()
    assert secret not in str(captured.value)
    assert "token" not in str(captured.value)
    assert captured.value.__cause__ is None
    path.write_text(json.dumps(["leaked-array-item"]), encoding="utf-8")
    with pytest.raises(AtomicJsonStateStoreError) as captured:
        store.load()
    assert "leaked-array-item" not in str(captured.value)


def test_non_mapping_and_non_json_types_are_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    secret = "leaked-password"
    with pytest.raises(AtomicJsonStateStoreError) as captured:
        store.save([secret])  # type: ignore[arg-type]
    assert secret not in str(captured.value)
    with pytest.raises(AtomicJsonStateStoreError) as captured:
        store.save({"when": datetime(2026, 1, 1), "password": secret})
    assert secret not in str(captured.value)
    assert "2026" not in str(captured.value)
    assert "password" not in str(captured.value)


def test_save_accepts_mapping_proxy_type(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = MappingProxyType({"name": "fleet", "count": 2, "nested": MappingProxyType({"k": "v"})})
    store.save(payload)
    assert store.load() == {"name": "fleet", "count": 2, "nested": {"k": "v"}}


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_load_rejects_non_finite_json_constants(tmp_path: Path, literal: str) -> None:
    path = tmp_path / "runtime-state.json"
    path.write_text('{"value": ' + literal + "}\n", encoding="utf-8")
    store = AtomicJsonStateStore(path)
    with pytest.raises(AtomicJsonStateStoreError) as captured:
        store.load()
    message = str(captured.value)
    assert literal not in message
    assert "nan" not in message.lower()
    assert "inf" not in message.lower()
    assert captured.value.__cause__ is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_rejected(tmp_path: Path, value: float) -> None:
    store = _store(tmp_path)
    with pytest.raises(AtomicJsonStateStoreError) as captured:
        store.save({"value": value})
    message = str(captured.value)
    assert "nan" not in message.lower()
    assert "inf" not in message.lower()
    assert captured.value.__cause__ is None
    assert store.load() is None


def test_saved_file_mode_is_0600(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save({"ok": True})
    assert (tmp_path / "runtime-state.json").stat().st_mode & 0o777 == 0o600


def test_failed_replace_keeps_old_file_and_cleans_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    original = {"generation": 1, "keep": True}
    store.save(original)
    target = tmp_path / "runtime-state.json"

    def boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        store.save({"generation": 2, "keep": False})
    assert json.loads(target.read_text(encoding="utf-8")) == original
    assert [path for path in tmp_path.iterdir() if path.name != target.name] == []
