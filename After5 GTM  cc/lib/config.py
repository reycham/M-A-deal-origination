"""Centralised env loading. Import `get_key` from anywhere; .env at repo root is auto-loaded."""
from __future__ import annotations
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass


def get_key(name: str, required: bool = True) -> str | None:
    val = os.environ.get(name)
    if required and not val:
        raise RuntimeError(f"Missing env var: {name}. Set it in .env at repo root.")
    return val
