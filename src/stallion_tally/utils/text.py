"""Text helpers: slugs and filesystem/blob-safe names."""

from __future__ import annotations

import re
import unicodedata

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_SPACE = re.compile(r"\s+")


def safe_name(value: str, fallback: str = "unnamed", max_length: int = 120) -> str:
    """Make a human readable name safe for local folders and blob paths.

    Keeps letters, digits, spaces and common punctuation; strips characters
    that are illegal on Windows or ambiguous in blob paths.
    """
    text = unicodedata.normalize("NFKC", value or "")
    text = _UNSAFE_CHARS.sub("", text)
    text = _MULTI_SPACE.sub(" ", text).strip(" .")
    if not text:
        return fallback
    return text[:max_length].rstrip(" .") or fallback


def slugify(value: str, fallback: str = "unnamed") -> str:
    """Lower-case ASCII slug (used for stable identifiers)."""
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or fallback


def clean_text(value: str | None) -> str | None:
    """Strip surrounding whitespace; return None for empty strings."""
    if value is None:
        return None
    text = value.strip()
    return text or None
