from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "dependency_quarantine.py"
_SPEC = importlib.util.spec_from_file_location("dependency_quarantine", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
dependency_quarantine = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = dependency_quarantine
_SPEC.loader.exec_module(dependency_quarantine)

QuarantineError = dependency_quarantine.QuarantineError
load_policy = dependency_quarantine.load_policy
verify_lock = dependency_quarantine.verify_lock
verify_repository = dependency_quarantine.verify_repository


def _write_project(
    root: Path,
    project: Path,
    *,
    duration: str = "72 hours",
    upload_time: str = "2026-07-30T00:00:00Z",
    source: str = 'registry = "https://pypi.org/simple"',
) -> None:
    directory = root / project
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pyproject.toml").write_text(
        f'[project]\nname = "fixture"\nversion = "0.1.0"\n\n'
        f'[tool.uv]\nexclude-newer = "{duration}"\n',
        encoding="utf-8",
    )
    (directory / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n'
        '[options]\nexclude-newer = "2026-07-31T00:00:00Z"\n'
        'exclude-newer-span = "PT72H"\n\n'
        "[[package]]\n"
        'name = "fixture-dependency"\n'
        'version = "1.2.3"\n'
        f"source = {{ {source} }}\n"
        "wheels = [\n"
        '  { url = "https://files.pythonhosted.org/fixture.whl", '
        f'hash = "sha256:abc", size = 1, upload-time = "{upload_time}" }},\n'
        "]\n",
        encoding="utf-8",
    )


def test_repository_policy_and_locked_artifacts_pass_at_strictly_older_age(
    tmp_path: Path,
) -> None:
    for project in dependency_quarantine.PROJECTS:
        _write_project(tmp_path, project)

    assert verify_repository(tmp_path, now=datetime(2026, 8, 3, tzinfo=UTC)) == (4, 4)


def test_lock_rejects_artifact_exactly_at_cutoff(tmp_path: Path) -> None:
    _write_project(tmp_path, Path("."), upload_time="2026-07-31T00:00:00Z")
    policy = load_policy(tmp_path, Path("."))

    with pytest.raises(QuarantineError, match="not strictly older"):
        verify_lock(tmp_path, policy, now=datetime(2026, 8, 3, tzinfo=UTC))


def test_policy_rejects_short_or_package_specific_bypasses(tmp_path: Path) -> None:
    _write_project(tmp_path, Path("."), duration="71 hours")
    with pytest.raises(QuarantineError, match="below the 72-hour minimum"):
        load_policy(tmp_path, Path("."))

    _write_project(tmp_path, Path("."))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8") + "\nexclude-newer-package = { unsafe = false }\n",
        encoding="utf-8",
    )
    with pytest.raises(QuarantineError, match="bypasses are forbidden"):
        load_policy(tmp_path, Path("."))


def test_lock_fails_closed_without_registry_timestamp(tmp_path: Path) -> None:
    _write_project(tmp_path, Path("."), upload_time="")
    policy = load_policy(tmp_path, Path("."))

    with pytest.raises(QuarantineError, match="missing registry upload-time"):
        verify_lock(tmp_path, policy, now=datetime(2026, 8, 3, tzinfo=UTC))


def test_lock_fails_closed_for_non_registry_dependency(tmp_path: Path) -> None:
    _write_project(tmp_path, Path("."), source='git = "https://example.invalid/repo"')
    policy = load_policy(tmp_path, Path("."))

    with pytest.raises(QuarantineError, match="non-registry dependency"):
        verify_lock(tmp_path, policy, now=datetime(2026, 8, 3, tzinfo=UTC))
