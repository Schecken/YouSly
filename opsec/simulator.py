from __future__ import annotations

import random
import re
import time
from typing import Callable, List, Optional, Set

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
    "vs",
    "what",
}


def normalize_topic_phrase(topic: str) -> str:
    return re.sub(r"\s+", " ", (topic or "").strip().lower())


def tokenize_topic_text(topic: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (topic or "").lower()) if t and t not in STOPWORDS]


class YouTubeOpSecSimulator:
    def __init__(
        self,
        level: int = 0,
        show_progress: bool = True,
        verbosity: int = 0,
        info_fn: Optional[Callable[[bool, str], None]] = None,
        debug_fn: Optional[Callable[[int, str, int], None]] = None,
    ):
        self.level = level
        self.show_progress = show_progress
        self.verbosity = verbosity
        self._info_fn = info_fn
        self._debug_fn = debug_fn

    def enabled(self) -> bool:
        return self.level > 0

    def _info(self, message: str):
        if self._info_fn:
            self._info_fn(self.show_progress, message)

    def _debug(self, message: str, level: int = 1):
        if self._debug_fn:
            self._debug_fn(self.verbosity, message, level)

    def sleep_with_jitter(self, base: float = 0.8, jitter: float = 1.2, stage: str = ""):
        if self.level < 1:
            return
        delay = base + random.uniform(0, jitter)
        self._debug(f"opsec sleep: {delay:.2f}s stage={stage or 'generic'}", level=2)
        time.sleep(delay)

    def _typo_token(self, token: str) -> str:
        if len(token) < 4:
            return token
        mode = random.choice(["double", "drop", "swap", "replace"])
        idx = random.randint(1, len(token) - 2)
        if mode == "double":
            return token[:idx] + token[idx] + token[idx:]
        if mode == "drop":
            return token[:idx] + token[idx + 1 :]
        if mode == "swap":
            chars = list(token)
            chars[idx], chars[idx + 1] = chars[idx + 1], chars[idx]
            return "".join(chars)
        repl = random.choice("abcdefghijklmnopqrstuvwxyz")
        return token[:idx] + repl + token[idx + 1 :]

    def _build_noise_queries(self, topic: Optional[str], level: int) -> List[str]:
        base = normalize_topic_phrase(topic or "videos")
        topic_tokens = tokenize_topic_text(base)
        if not topic_tokens:
            topic_tokens = [w for w in base.split() if w]
        seed = " ".join(topic_tokens[:4]) if topic_tokens else "videos"

        queries: List[str] = []
        seen: Set[str] = set()

        def add(q: str):
            q = normalize_topic_phrase(q)
            if q and q not in seen:
                seen.add(q)
                queries.append(q)

        suffixes = [
            "highlights",
            "best moments",
            "analysis",
            "interview",
            "behind the scenes",
            "fan reactions",
            "clip compilation",
            "explained",
        ]
        for s in suffixes:
            add(f"{seed} {s}")
            if len(topic_tokens) >= 2:
                add(f"{' '.join(topic_tokens[:2])} {s}")

        for t in topic_tokens[:3]:
            add(t)
            add(f"{t} latest")
            add(f"{t} shorts")

        if level >= 2:
            typo_tokens = [self._typo_token(t) for t in topic_tokens[:4]]
            typo_seed = " ".join(typo_tokens) if typo_tokens else self._typo_token(seed)
            add(f"{typo_seed} highlights")
            add(f"{typo_seed} best clips")
            add(f"{typo_seed} reacts")
            if topic_tokens:
                one_off = topic_tokens[:]
                mutate_idx = random.randrange(len(one_off))
                one_off[mutate_idx] = self._typo_token(one_off[mutate_idx])
                add(" ".join(one_off))

        if not queries:
            add("trending videos")
            add("popular clips")
        return queries

    def simulate_noise(self, yt_client, topic: Optional[str], technique: str):
        del technique
        if self.level < 1:
            return
        self._info("OPSEC L1: running noise searches and lightweight previews.")
        noise_queries = self._build_noise_queries(topic, self.level)
        sample_n = 2 if self.level == 1 else 4
        for q in random.sample(noise_queries, min(sample_n, len(noise_queries))):
            self.sleep_with_jitter(base=0.2, jitter=0.6, stage="noise-search")
            results = yt_client.search_videos(
                q,
                max_results=random.randint(4, 10 if self.level == 1 else 14),
                debug=self.verbosity,
                page=1,
            )
            if not results:
                continue
            preview_n = 1 if self.level == 1 else 3
            for item in random.sample(results, min(preview_n, len(results))):
                vid = item.get("id", {}).get("videoId")
                if not vid:
                    continue
                self.sleep_with_jitter(base=0.2, jitter=0.5, stage="preview")
                yt_client.search_related(
                    vid,
                    max_results=random.randint(2, 6 if self.level == 1 else 10),
                    debug=self.verbosity,
                    seed_title=item.get("snippet", {}).get("title", ""),
                    seed_creator=item.get("snippet", {}).get("channelTitle", ""),
                    topic=topic,
                )

    def simulate_human_browsing(self, yt_client, topic: Optional[str]):
        if self.level < 2:
            return
        self._info("OPSEC L2: simulating extra browsing behavior.")
        topic_seed = normalize_topic_phrase(topic or "videos")
        browse_queries = [
            f"{topic_seed} playlist",
            f"{topic_seed} documentary",
            f"{topic_seed} top 10",
            f"{topic_seed} reaction",
            f"{topic_seed} shorts",
            f"{topic_seed} live",
        ]
        for q in random.sample(browse_queries, min(3, len(browse_queries))):
            self.sleep_with_jitter(base=0.3, jitter=0.8, stage="browse")
            yt_client.search_videos(q, max_results=random.randint(6, 12), debug=self.verbosity, page=1)

    def maybe_add_remove_mistake(
        self,
        writer,
        playlist_id: str,
        encoded_video_ids: Set[str],
        yt_client,
        topic: Optional[str],
    ):
        if self.level < 2:
            return
        if random.random() > 0.30:
            return
        self._info("OPSEC L2: simulating add/remove mistake.")
        mistake_query = normalize_topic_phrase(topic or "trending now")
        results = yt_client.search_videos(
            f"{mistake_query} random clip",
            max_results=8,
            debug=self.verbosity,
            page=1,
        )
        candidates = [
            x.get("id", {}).get("videoId")
            for x in results
            if x.get("id", {}).get("videoId") and x.get("id", {}).get("videoId") not in encoded_video_ids
        ]
        if not candidates:
            return
        wrong_vid = random.choice(candidates)
        item_id = writer.add_video_to_playlist_get_item_id(playlist_id, wrong_vid)
        if not item_id:
            return
        self.sleep_with_jitter(base=0.2, jitter=0.7, stage="realise-mistake")
        writer.remove_playlist_item(item_id)
