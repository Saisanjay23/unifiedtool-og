"""
Runtime configuration and diagnostics shared across entrypoints.
"""

from __future__ import annotations

import asyncio
import os
import platform
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIN_SUPPORTED_PYTHON = (3, 11)
RECOMMENDED_PYTHON = (3, 11)


def _version_label(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def configure_runtime() -> None:
    """
    Apply deterministic runtime configuration before the app creates event loops.
    """
    if sys.platform == "win32" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
        current_policy = asyncio.get_event_loop_policy()
        desired_policy = asyncio.WindowsProactorEventLoopPolicy
        if not isinstance(current_policy, desired_policy):
            asyncio.set_event_loop_policy(desired_policy())


def validate_python_runtime(raise_on_error: bool = False) -> list[str]:
    """
    Validate the active Python version and return non-fatal warnings.
    """
    current = sys.version_info[:2]
    warnings: list[str] = []

    if current < MIN_SUPPORTED_PYTHON:
        message = (
            f"Unsupported Python {sys.version.split()[0]}. "
            f"Use Python >= {_version_label(MIN_SUPPORTED_PYTHON)} "
            f"(recommended {_version_label(RECOMMENDED_PYTHON)})."
        )
        if raise_on_error:
            raise RuntimeError(message)
        warnings.append(message)
        return warnings

    if current != RECOMMENDED_PYTHON:
        warnings.append(
            f"Python {sys.version.split()[0]} is supported, but "
            f"{_version_label(RECOMMENDED_PYTHON)} is the recommended version "
            f"for the most consistent Playwright and asyncio behavior."
        )

    return warnings


def get_configured_worker_count() -> int:
    """
    Best-effort worker count detection for deployments that expose it via env vars.
    """
    for env_name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw_value = os.environ.get(env_name)
        if raw_value and raw_value.isdigit():
            return max(1, int(raw_value))
    return 1


def get_runtime_info() -> dict[str, Any]:
    """
    Runtime metadata used for diagnostics and health reporting.
    """
    warnings = validate_python_runtime(raise_on_error=False)
    worker_count = get_configured_worker_count()

    if worker_count > 1:
        warnings.append(
            "This tool keeps live job and login state in memory, so it must run "
            "with a single application worker for consistent behavior."
        )

    return {
        "python_version": sys.version.split()[0],
        "recommended_python": _version_label(RECOMMENDED_PYTHON),
        "supported_python": f">={_version_label(MIN_SUPPORTED_PYTHON)}",
        "platform": platform.platform(),
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "project_root": str(PROJECT_ROOT),
        "configured_workers": worker_count,
        "single_process_required": True,
        "warnings": warnings,
    }
