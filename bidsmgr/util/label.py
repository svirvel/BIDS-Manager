"""Small shared string-to-BIDS-label helpers."""

from __future__ import annotations

import re

_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-z]+")


def sanitize_bids_label(value: str) -> str:
    """Strip ``value`` down to the alphanumeric characters BIDS labels allow."""
    return _NON_ALNUM_RE.sub("", value or "")


__all__ = ["sanitize_bids_label"]
