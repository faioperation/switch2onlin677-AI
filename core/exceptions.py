"""
core/exceptions.py
==================
Typed application exception hierarchy.

Every exception maps to exactly one HTTP status code.
Routers raise these; global handlers in main.py convert them to
the standard JSON envelope:  {"success": false, "error": "<message>"}

Hierarchy
---------
AppError (base)
  ├── NotFoundError       404  — resource does not exist
  ├── ConflictError       409  — duplicate / uniqueness violation
  ├── AppValidationError  422  — bad input from the caller
  ├── ForbiddenError      403  — caller lacks permission
  └── ServiceError        500  — unrecoverable internal failure

Usage
-----
    from core.exceptions import NotFoundError, ConflictError, AppValidationError

    raise NotFoundError(f"Product '{barcode}' not found.")
    raise ConflictError(f"Barcode '{barcode}' already exists.")
    raise AppValidationError("barcode must be 6–32 alphanumeric characters")
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for all application-level errors.

    All subclasses carry a `status_code` so the global exception handler
    can convert them to the correct HTTP response without any router-level
    try/except.
    """
    status_code: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class NotFoundError(AppError):
    """404 — the requested resource does not exist."""
    status_code = 404


class ConflictError(AppError):
    """409 — uniqueness / duplicate violation."""
    status_code = 409


class AppValidationError(AppError):
    """422 — the caller sent invalid input.

    Named AppValidationError (not ValidationError) to avoid shadowing
    Pydantic's ValidationError which is used extensively in schemas.
    """
    status_code = 422


class ForbiddenError(AppError):
    """403 — the caller is not allowed to perform this action."""
    status_code = 403


class ServiceError(AppError):
    """500 — unrecoverable internal failure (DB error, external API down, etc.)

    Use this only when you have caught a lower-level exception and want to
    surface a safe, non-leaking message to the caller.
    """
    status_code = 500
