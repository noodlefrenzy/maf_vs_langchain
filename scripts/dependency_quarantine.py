#!/usr/bin/env python3
"""Verify and refresh uv locks under the repository's release-age quarantine."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MINIMUM_RELEASE_AGE = timedelta(hours=72)
PROJECTS = (
    Path("."),
    Path("runners/maf"),
    Path("runners/langgraph"),
    Path("services/incident_mcp"),
)
_FRIENDLY_DURATION = re.compile(r"^(?P<count>[1-9][0-9]*)\s+(?P<unit>hours?|days?|weeks?)$")


class QuarantineError(ValueError):
    """Raised when project policy or locked registry metadata fails closed."""


@dataclass(frozen=True)
class ProjectPolicy:
    project: Path
    minimum_age: timedelta


def _parse_duration(value: Any, *, path: Path) -> timedelta:
    if not isinstance(value, str):
        raise QuarantineError(f"{path}: tool.uv.exclude-newer must be a rolling duration")
    match = _FRIENDLY_DURATION.fullmatch(value.strip().lower())
    if match is None:
        raise QuarantineError(
            f"{path}: tool.uv.exclude-newer must use integer hours, days, or weeks"
        )
    count = int(match.group("count"))
    unit = match.group("unit")
    if unit.startswith("hour"):
        return timedelta(hours=count)
    if unit.startswith("day"):
        return timedelta(days=count)
    return timedelta(weeks=count)


def load_policy(repository_root: Path, project: Path) -> ProjectPolicy:
    pyproject_path = repository_root / project / "pyproject.toml"
    try:
        document = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise QuarantineError(f"cannot read {pyproject_path}: {exc}") from exc
    uv = document.get("tool", {}).get("uv")
    if not isinstance(uv, dict):
        raise QuarantineError(f"{pyproject_path}: missing [tool.uv]")
    minimum_age = _parse_duration(uv.get("exclude-newer"), path=pyproject_path)
    if minimum_age < MINIMUM_RELEASE_AGE:
        raise QuarantineError(
            f"{pyproject_path}: release age {minimum_age} is below the 72-hour minimum"
        )
    if "exclude-newer-package" in uv:
        raise QuarantineError(
            f"{pyproject_path}: package-specific quarantine bypasses are forbidden"
        )
    indexes = uv.get("index", [])
    if indexes is not None and not isinstance(indexes, list):
        raise QuarantineError(f"{pyproject_path}: tool.uv.index must be an array of tables")
    for index in indexes or []:
        if not isinstance(index, dict):
            raise QuarantineError(f"{pyproject_path}: malformed tool.uv.index entry")
        if index.get("exclude-newer") is False:
            raise QuarantineError(f"{pyproject_path}: index quarantine bypasses are forbidden")
    return ProjectPolicy(project=project, minimum_age=minimum_age)


def _parse_timestamp(value: Any, *, location: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise QuarantineError(f"{location}: missing registry upload-time")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuarantineError(f"{location}: invalid registry upload-time {value!r}") from exc
    if timestamp.tzinfo is None:
        raise QuarantineError(f"{location}: registry upload-time must include a UTC offset")
    return timestamp.astimezone(UTC)


def verify_lock(
    repository_root: Path,
    policy: ProjectPolicy,
    *,
    now: datetime,
) -> int:
    lock_path = repository_root / policy.project / "uv.lock"
    try:
        document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise QuarantineError(f"cannot read {lock_path}: {exc}") from exc
    packages = document.get("package")
    if not isinstance(packages, list):
        raise QuarantineError(f"{lock_path}: package list is missing")
    cutoff = now.astimezone(UTC) - policy.minimum_age
    options = document.get("options")
    if not isinstance(options, dict):
        raise QuarantineError(f"{lock_path}: quarantine lock options are missing")
    locked_cutoff = _parse_timestamp(
        options.get("exclude-newer"), location=f"{lock_path}: options.exclude-newer"
    )
    if locked_cutoff > cutoff:
        raise QuarantineError(
            f"{lock_path}: locked cutoff {locked_cutoff.isoformat()} is newer than the "
            f"current policy cutoff {cutoff.isoformat()}"
        )
    if not isinstance(options.get("exclude-newer-span"), str):
        raise QuarantineError(f"{lock_path}: rolling quarantine span is missing")
    checked = 0
    for package in packages:
        if not isinstance(package, dict):
            raise QuarantineError(f"{lock_path}: malformed package entry")
        source = package.get("source")
        if not isinstance(source, dict):
            raise QuarantineError(f"{lock_path}: package source is missing")
        if "editable" in source or "virtual" in source:
            continue
        registry = source.get("registry")
        name = package.get("name", "<unknown>")
        version = package.get("version", "<unknown>")
        location = f"{lock_path}: {name}=={version}"
        if not isinstance(registry, str):
            raise QuarantineError(
                f"{location}: non-registry dependency has no authoritative publication timestamp"
            )
        artifacts: list[dict[str, Any]] = []
        sdist = package.get("sdist")
        if sdist is not None:
            if not isinstance(sdist, dict):
                raise QuarantineError(f"{location}: malformed sdist metadata")
            artifacts.append(sdist)
        wheels = package.get("wheels", [])
        if not isinstance(wheels, list) or any(not isinstance(wheel, dict) for wheel in wheels):
            raise QuarantineError(f"{location}: malformed wheel metadata")
        artifacts.extend(wheels)
        if not artifacts:
            raise QuarantineError(f"{location}: no locked registry artifacts")
        for artifact in artifacts:
            artifact_url = artifact.get("url", "<unknown artifact>")
            uploaded_at = _parse_timestamp(
                artifact.get("upload-time"), location=f"{location}: {artifact_url}"
            )
            if uploaded_at >= cutoff:
                raise QuarantineError(
                    f"{location}: artifact uploaded {uploaded_at.isoformat()} is not strictly "
                    f"older than cutoff {cutoff.isoformat()}"
                )
            checked += 1
    return checked


def verify_repository(repository_root: Path, *, now: datetime | None = None) -> tuple[int, int]:
    resolved_now = (now or datetime.now(UTC)).astimezone(UTC)
    policies = [load_policy(repository_root, project) for project in PROJECTS]
    checked = sum(verify_lock(repository_root, policy, now=resolved_now) for policy in policies)
    return len(policies), checked


def _check_locks(repository_root: Path) -> None:
    for project in PROJECTS:
        subprocess.run(
            ["uv", "lock", "--check", "--project", str(repository_root / project)],
            cwd=repository_root,
            check=True,
        )


def _refresh_locks(repository_root: Path) -> None:
    for project in PROJECTS:
        subprocess.run(
            ["uv", "lock", "--project", str(repository_root / project)],
            cwd=repository_root,
            check=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "lock"))
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args(argv)
    repository_root = args.repository_root.resolve()
    try:
        if args.action == "lock":
            _refresh_locks(repository_root)
        project_count, artifact_count = verify_repository(repository_root)
        _check_locks(repository_root)
    except (QuarantineError, subprocess.CalledProcessError) as exc:
        print(f"dependency quarantine failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"dependency quarantine passed: {project_count} projects, "
        f"{artifact_count} registry artifacts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
