"""Earth Engine client utilities: timeout-enforced execution, error handling, and memory-limit protection."""

import concurrent.futures
import logging
from typing import Any, Callable, Optional

import ee
from fastapi import HTTPException

from app.config import EE_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# Shared executor for blocking Earth Engine operations
_ee_executor = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="ee_worker")


def run_with_timeout(
    func: Callable[..., Any],
    *args: Any,
    timeout_seconds: Optional[float] = None,
    **kwargs: Any,
) -> Any:
    """
    Run a blocking Earth Engine function in a thread with a hard timeout.

    Raises:
        TimeoutError: If execution exceeds timeout_seconds.
        Exception: Any exception raised by the underlying function.
    """
    timeout = timeout_seconds if timeout_seconds is not None else EE_TIMEOUT_SECONDS
    future = _ee_executor.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        logger.warning("Earth Engine operation timed out after %.1f seconds", timeout)
        raise TimeoutError(
            f"Earth Engine operation timed out after {timeout:.1f} seconds. "
            "Please narrow your date range or submit an asynchronous batch export via /api/exports/batch."
        ) from exc


def handle_ee_error(exc: Exception) -> HTTPException:
    """
    Map Earth Engine exceptions to appropriate HTTPExceptions with actionable messages.
    """
    err_str = str(exc)
    logger.error("Earth Engine error encountered: %s (%s)", err_str, type(exc).__name__)

    if isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError)):
        return HTTPException(
            status_code=504,
            detail=f"Earth Engine request timed out after {EE_TIMEOUT_SECONDS:.1f}s. "
                   "For large date ranges or historical analyses, please use asynchronous batch export (/api/exports/batch).",
        )

    if "User memory limit exceeded" in err_str or "memory limit" in err_str.lower():
        return HTTPException(
            status_code=413,
            detail="Earth Engine memory limit exceeded. Narrow the date range, filter data server-side, "
                   "or submit an asynchronous batch export via /api/exports/batch for large historical analyses.",
        )

    if "Computation timed out" in err_str:
        return HTTPException(
            status_code=504,
            detail="Earth Engine computation timed out on Google servers. Narrow the date range or use batch export.",
        )

    return HTTPException(
        status_code=502,
        detail=f"Earth Engine request failed: {err_str}",
    )


def safe_get_info(ee_object: Any, timeout_seconds: Optional[float] = None, default: Any = None) -> Any:
    """
    Safely evaluate an Earth Engine object with getInfo() under a strict timeout.
    Returns default if ee_object is None.
    """
    if ee_object is None:
        return default
    return run_with_timeout(ee_object.getInfo, timeout_seconds=timeout_seconds)
