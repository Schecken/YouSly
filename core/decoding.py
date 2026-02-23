from __future__ import annotations

"""Decoding domain helpers for YouSly.

Core decode orchestration still lives in yously.py for now.
"""

from typing import List


def decode_values(values: List[str], key_positions: List[int]) -> str:
    out = []
    if not key_positions:
        return ""
    for idx, value in enumerate(values):
        pos = key_positions[idx % len(key_positions)]
        out.append(value[pos] if pos < len(value) else "?")
    return "".join(out)
