"""
platform_operator/helm/helm_client.py
Helm 3 subprocess wrapper with structured output parsing, retry logic,
and detailed logging. All Helm interactions go through this module.
"""

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Path to helm-values directory (relative to project root, resolved at runtime)
HELM_VALUES_DIR = Path(__file__).parents[2] / "helm-values"


class HelmError(Exception):
    """Raised when a Helm command exits with a non-zero code."""
    def __init__(self, message: str, returncode: int, stderr: str):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def _run(args: List[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """Execute a helm command and return the CompletedProcess result."""
    cmd = ["helm"] + args
    logger.debug("Executing: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        logger.error("Helm command failed (rc=%d): %s", result.returncode, result.stderr)
        raise HelmError(
            f"Helm command failed: {' '.join(cmd[:4])}",
            returncode=result.returncode,
            stderr=result.stderr,
        )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def helm_install(
    release: str,
    chart: str,
    namespace: str,
    values_file: Optional[str] = None,
    set_values: Optional[Dict[str, Any]] = None,
    version: Optional[str] = None,
    create_namespace: bool = True,
    timeout: str = "10m",
    atomic: bool = True,
) -> Dict[str, Any]:
    """
    Run `helm install`. Returns the parsed JSON status dict.

    Args:
        release:          Helm release name
        chart:            Chart reference (repo/name)
        namespace:        Target Kubernetes namespace
        values_file:      Path to a values YAML file (optional)
        set_values:       Dict of --set key=value overrides
        version:          Chart version pinning
        create_namespace: Pass --create-namespace flag
        timeout:          Helm install timeout (e.g. '10m')
        atomic:           Roll back on failure
    """
    args = [
        "install", release, chart,
        "--namespace", namespace,
        "--timeout", timeout,
        "--output", "json",
    ]
    if create_namespace:
        args.append("--create-namespace")
    if atomic:
        args.append("--atomic")
    if version:
        args.extend(["--version", version])
    if values_file:
        vf = HELM_VALUES_DIR / values_file if not Path(values_file).is_absolute() else Path(values_file)
        if vf.exists():
            args.extend(["-f", str(vf)])
    if set_values:
        for k, v in set_values.items():
            args.extend(["--set", f"{k}={v}"])

    logger.info("Installing Helm release '%s' (chart=%s, ns=%s)", release, chart, namespace)
    result = _run(args, timeout=660)
    status = json.loads(result.stdout) if result.stdout.strip() else {}
    logger.info("Release '%s' installed successfully", release)
    return status


def helm_upgrade(
    release: str,
    chart: str,
    namespace: str,
    values_file: Optional[str] = None,
    set_values: Optional[Dict[str, Any]] = None,
    version: Optional[str] = None,
    install: bool = True,
    create_namespace: bool = True,
    timeout: str = "10m",
    atomic: bool = False,
) -> Dict[str, Any]:
    """
    Run `helm upgrade [--install]`. Idempotent — installs if not present.
    """
    args = [
        "upgrade", release, chart,
        "--namespace", namespace,
        "--timeout", timeout,
        "--output", "json",
    ]
    if install:
        args.append("--install")
    if create_namespace:
        args.append("--create-namespace")
    if atomic:
        args.append("--atomic")
    if version:
        args.extend(["--version", version])
    if values_file:
        vf = HELM_VALUES_DIR / values_file if not Path(values_file).is_absolute() else Path(values_file)
        if vf.exists():
            args.extend(["-f", str(vf)])
    if set_values:
        for k, v in set_values.items():
            args.extend(["--set", f"{k}={v}"])

    logger.info("Upgrading/Installing Helm release '%s' (chart=%s, ns=%s)", release, chart, namespace)
    result = _run(args, timeout=660)
    status = json.loads(result.stdout) if result.stdout.strip() else {}
    logger.info("Release '%s' deployed/upgraded successfully", release)
    return status


def helm_uninstall(
    release: str,
    namespace: str,
    ignore_not_found: bool = True,
    timeout: str = "5m",
) -> bool:
    """
    Run `helm uninstall`. Returns True if uninstalled, False if not found
    (when ignore_not_found=True).
    """
    args = [
        "uninstall", release,
        "--namespace", namespace,
        "--timeout", timeout,
    ]
    logger.info("Uninstalling Helm release '%s' (ns=%s)", release, namespace)
    try:
        _run(args, timeout=360)
        logger.info("Release '%s' uninstalled", release)
        return True
    except HelmError as exc:
        if ignore_not_found and ("not found" in exc.stderr.lower() or "release: not found" in exc.stderr.lower()):
            logger.warning("Release '%s' not found — skipping uninstall", release)
            return False
        raise


def helm_status(release: str, namespace: str) -> Optional[Dict[str, Any]]:
    """
    Run `helm status`. Returns parsed dict or None if release not found.
    """
    try:
        result = _run(["status", release, "--namespace", namespace, "--output", "json"])
        return json.loads(result.stdout)
    except HelmError as exc:
        if "not found" in exc.stderr.lower():
            return None
        raise


def helm_release_exists(release: str, namespace: str) -> bool:
    """Return True if the Helm release exists (any status)."""
    return helm_status(release, namespace) is not None


def helm_is_deployed(release: str, namespace: str) -> bool:
    """Return True only if release is in 'deployed' state."""
    status = helm_status(release, namespace)
    if not status:
        return False
    info = status.get("info", {})
    return info.get("status", "").lower() == "deployed"


def helm_repo_update() -> None:
    """Refresh all Helm repository indexes."""
    logger.info("Updating Helm repositories...")
    _run(["repo", "update"])
    logger.info("Helm repositories updated")
