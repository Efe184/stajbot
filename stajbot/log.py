"""Loglama yardımcıları (paylaşılan sözleşme)."""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def _level_from_settings(default: int) -> int:
    """.env içindeki LOG_LEVEL (DEBUG/INFO/WARNING/ERROR) okunur; okunamazsa varsayılan."""
    try:
        from .config import get_settings

        name = str(get_settings().log_level or "").upper().strip()
        return getattr(logging, name, default) if name else default
    except Exception:  # noqa: BLE001 - ayar okunamazsa loglama yine çalışmalı
        return default


def setup_logging(level: int | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    if level is None:
        level = _level_from_settings(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger("stajbot")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    # gürültülü kütüphaneler
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    if not name.startswith("stajbot"):
        name = f"stajbot.{name}"
    return logging.getLogger(name)
