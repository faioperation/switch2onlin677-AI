"""
services/lead_service.py
========================
Saves user-expressed product interest to the external CRM leads API.
"""
from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

_LEADS_API_URL = os.getenv("LEADS_API_URL", "")


def save_lead(user_id: str, products: list[dict]) -> None:
    """
    POST the first product the user showed interest in to the leads CRM.
    Silently skips if LEADS_API_URL is not configured or the call fails.
    """
    if not products or not _LEADS_API_URL:
        return

    payload = {
        "user_id":          user_id,
        "interested_product": products[0].get("name", ""),
    }
    try:
        resp = requests.post(_LEADS_API_URL, json=payload, timeout=10)
        logger.info("lead_saved user=%s status=%s", user_id, resp.status_code)
    except Exception as exc:
        logger.warning("lead_save_failed user=%s error=%s", user_id, exc)
