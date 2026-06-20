"""
core/circuit_breaker.py
========================
Circuit breaker for OpenAI API calls.

WHY THIS EXISTS
---------------
Without a circuit breaker, if OpenAI returns 429 (rate limit), 500 (server
error), or times out, every waiting request will pile up, hold worker threads,
and eventually crash the server under load.

A circuit breaker detects repeated failures and "opens" the circuit — failing
fast with a clear error instead of waiting for timeouts. After a cooldown
period it allows test calls through to detect recovery.

STATES
------
  CLOSED  (normal)  : All requests pass through. Failure counter tracked.
  OPEN    (failing) : Fast-fail all requests immediately. No OpenAI calls made.
  HALF_OPEN (probe) : One test request allowed through. If it succeeds → CLOSED.
                      If it fails → back to OPEN with reset cooldown.

THRESHOLDS (configurable via env)
----------------------------------
  CB_FAILURE_THRESHOLD   = 5   consecutive failures before opening  (default: 5)
  CB_RECOVERY_TIMEOUT    = 60  seconds in OPEN state before probing (default: 60)
  CB_SUCCESS_THRESHOLD   = 2   consecutive probe successes to close (default: 2)

INTEGRATION
-----------
The orchestrator wraps every _call_gpt() with the circuit breaker:

    with openai_circuit_breaker:
        response = await client.chat.completions.create(...)

If the circuit is OPEN, raises CircuitOpenError immediately.
The chat route catches CircuitOpenError and returns a graceful degraded message.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

FAILURE_THRESHOLD  = int(os.getenv("CB_FAILURE_THRESHOLD",  "5"))
RECOVERY_TIMEOUT   = int(os.getenv("CB_RECOVERY_TIMEOUT",   "60"))
SUCCESS_THRESHOLD  = int(os.getenv("CB_SUCCESS_THRESHOLD",  "2"))


# ── Custom exception ──────────────────────────────────────────────────────────

class CircuitOpenError(RuntimeError):
    """Raised when the circuit breaker is OPEN and fast-failing requests."""

    def __init__(self, service: str, opens_at: float) -> None:
        self.service  = service
        self.opens_at = opens_at
        remaining = max(0.0, opens_at - time.monotonic())
        super().__init__(
            f"Circuit OPEN for '{service}'. "
            f"Recovers in {remaining:.0f}s. "
            "OpenAI API is temporarily unavailable."
        )


# ── State ─────────────────────────────────────────────────────────────────────

class _State(Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


# ── Circuit breaker ───────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Thread-safe circuit breaker for a named external service.

    Usage as a context manager:
        cb = CircuitBreaker("openai")

        # Synchronous
        with cb:
            result = call_api()

        # Async — use call_async():
        result = await cb.call_async(async_call_api)
    """

    def __init__(
        self,
        service:           str,
        failure_threshold: int = FAILURE_THRESHOLD,
        recovery_timeout:  int = RECOVERY_TIMEOUT,
        success_threshold: int = SUCCESS_THRESHOLD,
    ) -> None:
        self.service           = service
        self._failure_threshold = failure_threshold
        self._recovery_timeout  = recovery_timeout
        self._success_threshold = success_threshold

        self._state            = _State.CLOSED
        self._failure_count    = 0
        self._success_count    = 0
        self._opened_at: Optional[float] = None
        self._lock             = threading.Lock()

    # ── State inspection ──────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state.value

    @property
    def is_open(self) -> bool:
        return self._state == _State.OPEN

    def get_stats(self) -> dict:
        with self._lock:
            remaining = (
                max(0.0, self._opened_at + self._recovery_timeout - time.monotonic())
                if self._opened_at else 0.0
            )
            return {
                "service":          self.service,
                "state":            self._state.value,
                "failure_count":    self._failure_count,
                "success_count":    self._success_count,
                "recovery_in_sec":  round(remaining, 1),
            }

    # ── Internal transitions ──────────────────────────────────────────────────

    def _check_state(self) -> None:
        """Called before each call; may transition OPEN → HALF_OPEN."""
        with self._lock:
            if self._state == _State.OPEN:
                if time.monotonic() >= self._opened_at + self._recovery_timeout:
                    self._state         = _State.HALF_OPEN
                    self._success_count = 0
                    logger.info("circuit_half_open service=%s", self.service)
                else:
                    raise CircuitOpenError(
                        self.service,
                        self._opened_at + self._recovery_timeout,
                    )

    def _on_success(self) -> None:
        with self._lock:
            if self._state == _State.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._success_threshold:
                    self._state         = _State.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
                    self._opened_at     = None
                    logger.info("circuit_closed service=%s", self.service)
            elif self._state == _State.CLOSED:
                self._failure_count = 0   # reset on any success

    def _on_failure(self, exc: Exception) -> None:
        with self._lock:
            self._failure_count += 1
            if self._state == _State.HALF_OPEN:
                # Probe failed — reopen
                self._state     = _State.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "circuit_reopened service=%s probe_failed error=%s",
                    self.service, exc,
                )
            elif (
                self._state == _State.CLOSED
                and self._failure_count >= self._failure_threshold
            ):
                self._state     = _State.OPEN
                self._opened_at = time.monotonic()
                logger.error(
                    "circuit_opened service=%s failures=%d threshold=%d error=%s",
                    self.service, self._failure_count, self._failure_threshold, exc,
                )

    # ── Context manager (sync) ────────────────────────────────────────────────

    def __enter__(self) -> "CircuitBreaker":
        self._check_state()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            self._on_success()
        elif not isinstance(exc_val, CircuitOpenError):
            # Only track failures from the actual service, not our own error
            if _is_transient_error(exc_val):
                self._on_failure(exc_val)
            else:
                # Non-transient (validation error, etc.) — don't penalise circuit
                self._on_success()
        return False   # never suppress exceptions

    # ── Async call wrapper ────────────────────────────────────────────────────

    async def call_async(self, coro_factory: Callable, *args, **kwargs):
        """
        Execute an async callable protected by the circuit breaker.

            result = await cb.call_async(client.chat.completions.create, **params)
        """
        self._check_state()
        try:
            result = await coro_factory(*args, **kwargs)
            self._on_success()
            return result
        except CircuitOpenError:
            raise
        except Exception as exc:
            if _is_transient_error(exc):
                self._on_failure(exc)
            else:
                self._on_success()
            raise


# ── Transient error classifier ─────────────────────────────────────────────────

def _is_transient_error(exc: Exception) -> bool:
    """
    Return True for errors that indicate OpenAI is unavailable/overloaded.
    Return False for errors caused by our own code (bad payload, etc.).
    """
    exc_type = type(exc).__name__
    # OpenAI SDK exception types
    _TRANSIENT = {
        "RateLimitError",       # 429
        "APIStatusError",       # 5xx
        "APIConnectionError",   # network failure
        "APITimeoutError",      # timeout
        "ServiceUnavailableError",
        "InternalServerError",
    }
    if exc_type in _TRANSIENT:
        return True
    # Also catch by message for library-version variance
    msg = str(exc).lower()
    return any(s in msg for s in ("rate limit", "timeout", "503", "502", "500", "connection"))


# ── Global singleton ──────────────────────────────────────────────────────────

openai_circuit_breaker = CircuitBreaker(
    service           = "openai",
    failure_threshold = FAILURE_THRESHOLD,
    recovery_timeout  = RECOVERY_TIMEOUT,
    success_threshold = SUCCESS_THRESHOLD,
)
