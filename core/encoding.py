from __future__ import annotations

"""Encoding domain helpers for YouSly.

This module is intentionally light for now; encode orchestration still lives in yously.py.
"""

from typing import List


def build_position_candidates(base_pos: int, max_pos: int = 15) -> List[int]:
    domain = list(range(0, max_pos + 1))
    others = sorted((p for p in domain if p != base_pos), key=lambda p: abs(p - base_pos))
    return [base_pos] + others


def positions_to_key(positions: List[int]) -> str:
    return "".join(format(p, "x") for p in positions)
