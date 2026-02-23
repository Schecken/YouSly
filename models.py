from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PickedVideo:
    video_id: str
    title: str
    creator: str
    intended_char: str
    key_pos: int
    extracted_char: str
    match_type: str
    query_used: str
