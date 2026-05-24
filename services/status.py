"""
services/status.py
==================
Product status management — transition guard, audit logging, and editorial
flags service.

Architecture
------------
ProductStatusService  (class)
  Stateless helper constructed with an injected Session.
  All write methods call db.flush() but NOT db.commit() — the router
  commits after calling the service, so the whole request is one transaction.

Public API
----------
  .change_status(barcode, new_status, changed_by, reason)
      Validate transition → update product → write audit log → flush.
      Returns {"barcode", "from_status", "to_status", "changed_by", "reason"}.
      Raises NotFoundError (404) or AppValidationError (422).

  .bulk_change_status(barcodes, new_status, changed_by, reason)
      Same logic applied to a list of barcodes in one transaction.
      Returns {"updated": [...], "skipped": [...], "errors": [...]} summary.
      Partial success is allowed — invalid individual transitions are skipped.

  .update_flags(barcode, flags)
      Apply non-None editorial flags to one product → flush.
      Returns {"barcode", "updated_fields": {field: value, ...}}.
      Raises NotFoundError (404).

  .bulk_update_flags(barcodes, flags)
      Apply the same flags to multiple products in one UPDATE statement
      per changed field. Returns {"updated_count", "updated_fields"}.

Transition matrix
-----------------
            → draft   → active   → inactive
  draft       —         ✓          ✓
  active      ✓         —          ✓
  inactive    ✗         ✓          —

  inactive → draft is intentionally blocked: once a product was published
  and deactivated, reverting to draft is operationally confusing. Re-activate
  it (inactive → active) or leave it inactive.

Editorial flags (never overwritten by SAP sync)
-----------------------------------------------
  is_recommended, is_new_arrival, is_best_selling, is_cod_recommended,
  recommendation_priority, recommendation_score_override,
  price_tier, brand_family, best_selling_scope
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from core.exceptions import AppValidationError, NotFoundError
from models import Product, ProductStatus, ProductStatusLog
from schemas.status import FlagsUpdateRequest

logger = logging.getLogger(__name__)

# ── Transition rules ──────────────────────────────────────────────────────────

# ALLOWED_TRANSITIONS[from_status] = {set of allowed to_status values}
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft":    {"active", "inactive"},
    "active":   {"draft",  "inactive"},
    "inactive": {"active"},
    # "inactive → draft" is intentionally absent
}

VALID_STATUSES = set(ALLOWED_TRANSITIONS.keys())

# Fields the service will write when updating editorial flags
_FLAG_FIELDS: dict[str, str] = {
    "is_recommended":               "is_recommended",
    "is_new_arrival":               "is_new_arrival",
    "is_best_selling":              "is_best_selling",
    "is_cod_recommended":           "is_cod_recommended",
    "recommendation_priority":      "recommendation_priority",
    "recommendation_score_override":"recommendation_score_override",
    "price_tier":                   "price_tier",
    "brand_family":                 "brand_family",
    "best_selling_scope":           "best_selling_scope",
}


# ── Service class ─────────────────────────────────────────────────────────────

class ProductStatusService:
    """
    Stateless service — receives an injected Session.
    All methods flush but do not commit (router commits).
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_product(self, barcode: str) -> Product:
        product = (
            self.db.query(Product)
            .filter(Product.barcode == barcode)
            .first()
        )
        if not product:
            raise NotFoundError(f"Product '{barcode}' not found.")
        return product

    def _current_status(self, product: Product) -> str:
        """Return the string value of product.product_status."""
        s = product.product_status
        if s is None:
            return "draft"
        # Could be a ProductStatus enum instance or a bare string
        return s.value if hasattr(s, "value") else str(s)

    def _validate_transition(self, from_status: str, to_status: str) -> None:
        """Raise AppValidationError for illegal transitions."""
        if to_status not in VALID_STATUSES:
            raise AppValidationError(
                f"'{to_status}' is not a valid status. "
                f"Valid values: {sorted(VALID_STATUSES)}."
            )
        allowed = ALLOWED_TRANSITIONS.get(from_status, set())
        if to_status not in allowed:
            if not allowed:
                raise AppValidationError(
                    f"Status '{from_status}' has no allowed outgoing transitions."
                )
            raise AppValidationError(
                f"Transition '{from_status}' → '{to_status}' is not allowed. "
                f"From '{from_status}' you may transition to: "
                f"{sorted(allowed)}."
            )

    def _write_log(
        self,
        barcode:     str,
        from_status: Optional[str],
        to_status:   str,
        changed_by:  str,
        reason:      Optional[str],
    ) -> None:
        """Insert one audit log row (not flushed here; caller flushes)."""
        self.db.add(ProductStatusLog(
            barcode     = barcode,
            from_status = from_status,
            to_status   = to_status,
            changed_by  = changed_by or "system",
            reason      = reason,
        ))

    # ── Public methods ────────────────────────────────────────────────────────

    def change_status(
        self,
        barcode:    str,
        new_status: str,
        changed_by: Optional[str] = None,
        reason:     Optional[str] = None,
    ) -> dict:
        """
        Transition a single product to a new status.

        Raises
        ------
        NotFoundError      (404) — product doesn't exist
        AppValidationError (422) — invalid or disallowed transition
        """
        product     = self._get_product(barcode)
        from_status = self._current_status(product)

        if from_status == new_status:
            return {
                "barcode":     barcode,
                "from_status": from_status,
                "to_status":   new_status,
                "changed_by":  changed_by or "system",
                "reason":      reason,
                "note":        "Status unchanged — already in that state.",
            }

        self._validate_transition(from_status, new_status)

        product.product_status = new_status
        self._write_log(barcode, from_status, new_status, changed_by or "system", reason)
        self.db.flush()

        logger.info(
            "STATUS: '%s' %s → %s (by %s)",
            barcode, from_status, new_status, changed_by or "system",
        )

        return {
            "barcode":     barcode,
            "from_status": from_status,
            "to_status":   new_status,
            "changed_by":  changed_by or "system",
            "reason":      reason,
        }

    def bulk_change_status(
        self,
        barcodes:   list[str],
        new_status: str,
        changed_by: Optional[str] = None,
        reason:     Optional[str] = None,
    ) -> dict:
        """
        Transition multiple products to a new status in one transaction.

        Products that don't exist or have illegal transitions are collected
        in 'skipped' / 'errors' — the valid ones are still updated.

        Returns
        -------
        {
          "updated": [{"barcode": ..., "from_status": ...}, ...],
          "skipped":  [{"barcode": ..., "note": ...}, ...],   # already in state
          "errors":   [{"barcode": ..., "error": ...}, ...],  # not found / bad transition
          "summary":  {"updated": N, "skipped": N, "errors": N}
        }
        """
        if new_status not in VALID_STATUSES:
            raise AppValidationError(
                f"'{new_status}' is not a valid status. "
                f"Valid values: {sorted(VALID_STATUSES)}."
            )

        # Fetch all products in one query
        products: list[Product] = (
            self.db.query(Product)
            .filter(Product.barcode.in_(barcodes))
            .all()
        )
        found_map = {p.barcode: p for p in products}

        updated: list[dict] = []
        skipped: list[dict] = []
        errors:  list[dict] = []

        for barcode in barcodes:
            product = found_map.get(barcode)
            if not product:
                errors.append({"barcode": barcode, "error": "Product not found."})
                continue

            from_status = self._current_status(product)

            if from_status == new_status:
                skipped.append({"barcode": barcode, "note": f"Already '{new_status}'."})
                continue

            allowed = ALLOWED_TRANSITIONS.get(from_status, set())
            if new_status not in allowed:
                errors.append({
                    "barcode": barcode,
                    "error": (
                        f"Transition '{from_status}' → '{new_status}' is not allowed. "
                        f"Allowed from '{from_status}': {sorted(allowed)}."
                    ),
                })
                continue

            product.product_status = new_status
            self._write_log(barcode, from_status, new_status, changed_by or "system", reason)
            updated.append({"barcode": barcode, "from_status": from_status})

        if updated:
            self.db.flush()
            logger.info(
                "BULK STATUS → %s: updated=%d skipped=%d errors=%d (by %s)",
                new_status, len(updated), len(skipped), len(errors), changed_by or "system",
            )

        return {
            "updated": updated,
            "skipped": skipped,
            "errors":  errors,
            "summary": {
                "to_status": new_status,
                "updated":   len(updated),
                "skipped":   len(skipped),
                "errors":    len(errors),
            },
        }

    def update_flags(
        self,
        barcode: str,
        flags:   FlagsUpdateRequest,
    ) -> dict:
        """
        Apply non-None editorial flag values to a single product.

        Only the fields explicitly set in the request are touched.
        Returns a dict of the fields that were actually changed.

        Raises
        ------
        NotFoundError (404) — product doesn't exist
        """
        product = self._get_product(barcode)

        updated_fields: dict = {}
        flags_dict = flags.model_dump(exclude_none=True)

        for field, attr in _FLAG_FIELDS.items():
            if field in flags_dict:
                new_val = flags_dict[field]
                old_val = getattr(product, attr, None)
                if old_val != new_val:
                    setattr(product, attr, new_val)
                    updated_fields[field] = new_val

        if updated_fields:
            self.db.flush()
            logger.info(
                "FLAGS updated for '%s': %s",
                barcode, list(updated_fields.keys()),
            )

        return {
            "barcode":        barcode,
            "updated_fields": updated_fields,
            "note": (
                "No changes applied — all values were already set."
                if not updated_fields else None
            ),
        }

    def bulk_update_flags(
        self,
        barcodes: list[str],
        flags:    FlagsUpdateRequest,
    ) -> dict:
        """
        Apply the same editorial flags to multiple products.

        Uses targeted UPDATE ... WHERE barcode IN (...) per field — one
        statement per changed field, not one per product.

        Returns
        -------
        {
          "found":          N,   # products that exist
          "not_found":      [...],  # barcodes that didn't resolve
          "updated_count":  N,
          "updated_fields": {field: value, ...},
        }
        """
        flags_dict = flags.model_dump(exclude_none=True)
        if not flags_dict:
            raise AppValidationError("No flag values provided — nothing to update.")

        # Verify which barcodes actually exist
        existing: list[Product] = (
            self.db.query(Product)
            .filter(Product.barcode.in_(barcodes))
            .all()
        )
        existing_barcodes = [p.barcode for p in existing]
        not_found = [b for b in barcodes if b not in set(existing_barcodes)]

        if not existing_barcodes:
            return {
                "found":          0,
                "not_found":      not_found,
                "updated_count":  0,
                "updated_fields": {},
            }

        # Apply flags per-field via setattr on the already-loaded ORM objects
        # (avoids a raw UPDATE so SQLAlchemy's unit-of-work stays consistent)
        updated_fields: dict = {}
        for field, attr in _FLAG_FIELDS.items():
            if field in flags_dict:
                new_val = flags_dict[field]
                for product in existing:
                    setattr(product, attr, new_val)
                updated_fields[field] = new_val

        if updated_fields:
            self.db.flush()
            logger.info(
                "BULK FLAGS updated for %d products: %s",
                len(existing_barcodes), list(updated_fields.keys()),
            )

        return {
            "found":          len(existing_barcodes),
            "not_found":      not_found,
            "updated_count":  len(existing_barcodes),
            "updated_fields": updated_fields,
        }
