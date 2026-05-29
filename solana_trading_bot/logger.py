"""Logger centralisé (console riche + fichier)."""

from __future__ import annotations

import logging
from pathlib import Path

try:
    from rich.logging import RichHandler

    _HAS_RICH = True
except Exception:
    _HAS_RICH = False


_CONFIGURED = False


def setup_logging(log_path: str = "bot.log", level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger("bot")
    root.setLevel(log_level)
    root.handlers.clear()

    # Console
    if _HAS_RICH:
        console_handler: logging.Handler = RichHandler(
            rich_tracebacks=True, show_path=False, markup=True
        )
        console_handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
    else:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
        )
    console_handler.setLevel(log_level)
    root.addHandler(console_handler)

    # Fichier
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
        )
    )
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"bot.{name}")
