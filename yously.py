#!/usr/bin/env python3
"""
Requirements:
  pip install google-api-python-client google-auth-oauthlib
  # optional no-API mode: pip install yt-dlp
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import string
import sys
import time
import traceback
from typing import Dict, List, Optional, Set, Tuple

from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from models import PickedVideo
from opsec.simulator import YouTubeOpSecSimulator

SUPPORTED_CHARS = set(string.ascii_lowercase + string.digits + " ")
YOUTUBE_API_KEY = "AIzaSyDF7j-RS5-UNGQuyjYaiYOhkZqnNXkx9cQ"
YOUTUBE_OAUTH_CLIENT_SECRETS = "client_secret.json"
YOUTUBE_OAUTH_TOKEN_FILE = "youtube_token.json"
YOUTUBE_WRITE_SCOPES = ["https://www.googleapis.com/auth/youtube"]
DEFAULT_MAX_SEARCH_CALLS = 15
SEARCH_RESULT_JITTER = 0.20
DEFAULT_RESULTS_PER_SEARCH = 15
MAX_TOPIC_VARIANTS = 14
MAX_KEY_POSITION = 15

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

def build_position_candidates(base_pos: int) -> List[int]:
    domain = list(range(0, MAX_KEY_POSITION + 1))
    others = sorted((p for p in domain if p != base_pos), key=lambda p: abs(p - base_pos))
    return [base_pos] + others


def build_position_candidates_for_technique(base_pos: int, technique: str) -> List[int]:
    if technique == "videoid":
        domain = list(range(0, 11))
        base = base_pos % 11
        others = sorted((p for p in domain if p != base), key=lambda p: abs(p - base))
        return [base] + others
    return build_position_candidates(base_pos)


def positions_to_key(positions: List[int]) -> str:
    return "".join(format(p, "x") for p in positions)


def merge_effective_positions_into_key(
    original_key: str, effective_positions: List[int]
) -> str:
    merged = parse_key_to_positions(original_key)
    if not merged:
        return positions_to_key(effective_positions)

    seen_by_slot: Dict[int, int] = {}
    for i, pos in enumerate(effective_positions):
        slot = i % len(merged)
        prior = seen_by_slot.get(slot)
        if prior is None:
            seen_by_slot[slot] = pos
            merged[slot] = pos
            continue
        if prior != pos:
            raise ValueError(
                f"Conflicting effective key positions for slot {slot}: "
                f"{prior} vs {pos}. This cannot be represented by a looped key."
            )
    return positions_to_key(merged)


def otp():
    """
    Generate and return a 32-byte hexadecimal one-time pad key.
    """
    return ''.join(random.choices('0123456789abcdef', k=64))


def debug_log(verbosity: int, message: str, level: int = 1):
    if verbosity >= level:
        _emit_log("DEBUG", message, level=level)


def progress_log(enabled: bool, message: str):
    if enabled:
        _render_progress(message, include_stats=True)


def _compact_data(value: object, limit: int = 240) -> str:
    if value is None:
        return "-"
    text = str(value).replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "magenta": "\033[35m",
}

SPINNER_FRAMES = ["|", "/", "-", "\\"]

RUN_STATE = {
    "spinner_idx": 0,
    "found": 0,
    "indexed": 0,
    "active_progress": False,
    "show_opsec_line": False,
    "main_line": "",
    "opsec_line": "",
}


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.getenv("TERM", "") != "dumb"


def _color(text: str, name: str) -> str:
    if not _supports_color():
        return text
    return f"{COLORS.get(name, '')}{text}{COLORS['reset']}"


def _bold(text: str) -> str:
    if not _supports_color():
        return text
    return f"{COLORS['bold']}{text}{COLORS['reset']}"


def reset_run_state():
    RUN_STATE["spinner_idx"] = 0
    RUN_STATE["found"] = 0
    RUN_STATE["indexed"] = 0
    RUN_STATE["active_progress"] = False
    RUN_STATE["show_opsec_line"] = False
    RUN_STATE["main_line"] = ""
    RUN_STATE["opsec_line"] = ""


def set_opsec_progress_line(enabled: bool):
    RUN_STATE["show_opsec_line"] = bool(enabled)
    if not enabled:
        RUN_STATE["opsec_line"] = ""
    _render_progress_display()


def _clear_progress_line():
    if not RUN_STATE["active_progress"] or not sys.stdout.isatty():
        return
    if RUN_STATE["show_opsec_line"]:
        print("\x1b[1F", end="")  # move to first status line
        print("\r\033[2K", end="")  # clear main
        print("\n\r\033[2K", end="")  # clear opsec
    else:
        print("\r\033[2K", end="")
    print("\r", end="", flush=True)
    RUN_STATE["active_progress"] = False


def _emit_log(kind: str, message: str, level: int = 1):
    _clear_progress_line()
    label = kind.upper().ljust(7)
    color = "cyan" if kind == "DEBUG" else "magenta" if kind == "HTTP" else "green"
    lvl = f"L{level}" if kind == "DEBUG" else "--"
    print(f"{_color(label, color)} | {lvl} | {message}")


def progress_add_found(n: int):
    RUN_STATE["found"] = int(RUN_STATE["found"]) + max(0, n)


def progress_add_indexed(n: int):
    RUN_STATE["indexed"] = int(RUN_STATE["indexed"]) + max(0, n)


def _render_progress_display():
    main_line = RUN_STATE.get("main_line", "")
    opsec_line = RUN_STATE.get("opsec_line", "")
    show_opsec = bool(RUN_STATE.get("show_opsec_line", False))
    if not main_line and not (show_opsec and opsec_line):
        return
    if sys.stdout.isatty():
        if RUN_STATE["active_progress"] and show_opsec:
            print("\x1b[1F", end="")  # move from opsec line to main line
        print("\r\033[2K" + main_line, end="")
        if show_opsec:
            print("\n\r\033[2K" + opsec_line, end="")
        print("", end="", flush=True)
        RUN_STATE["active_progress"] = True
    else:
        print(f"[progress] {main_line}")
        if show_opsec:
            print(f"[opsec] {opsec_line}")


def _render_progress(message: str, include_stats: bool = True):
    frame = SPINNER_FRAMES[int(RUN_STATE["spinner_idx"]) % len(SPINNER_FRAMES)]
    RUN_STATE["spinner_idx"] = int(RUN_STATE["spinner_idx"]) + 1
    stats = f"found={RUN_STATE['found']:<4} indexed={RUN_STATE['indexed']:<3}"
    if include_stats:
        line = f"{_color(frame, 'green')} {message}  {_color(stats, 'dim')}"
    else:
        line = f"{_color(frame, 'cyan')} {message}"
    RUN_STATE["main_line"] = line
    _render_progress_display()


def progress_done():
    if RUN_STATE["active_progress"] and sys.stdout.isatty():
        print("")
    RUN_STATE["active_progress"] = False


def info_log(enabled: bool, message: str):
    if not enabled:
        return
    _render_progress(f"INFO: {message}", include_stats=False)


def opsec_log(enabled: bool, message: str):
    if not enabled:
        return
    RUN_STATE["opsec_line"] = f"{_color('O', 'yellow')} OPSEC: {message}"
    _render_progress_display()


def parse_http_error_reason(err: HttpError) -> str:
    try:
        payload = json.loads((err.content or b"{}").decode("utf-8"))
    except Exception:
        return ""
    errors = payload.get("error", {}).get("errors", [])
    if errors and isinstance(errors[0], dict):
        return str(errors[0].get("reason", ""))
    return ""


def http_log_request(
    verbosity: int,
    method: str,
    url: str,
    request_data: object = None,
    extra: str = "",
):
    if verbosity < 3:
        return
    suffix = f" | extra={_compact_data(extra)}" if extra else ""
    _emit_log(
        "HTTP",
        f"{method:<6} | {url} | data={_compact_data(request_data)}{suffix}",
    )


def _log_ytdlp_http_line(raw: str, verbosity: int):
    if verbosity < 3:
        return
    sent_match = re.search(r"send: b'([^']*)'", raw)
    if sent_match:
        payload = sent_match.group(1).replace("\\r\\n", " ").strip()
        if re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD)\s+", payload):
            _emit_log("HTTP", payload)
            return
    # Some urllib traces expose response headers on "reply:" lines.
    reply_match = re.search(r"reply:\s+'([^']+)'", raw)
    if reply_match:
        _emit_log("HTTP", f"REPLY  | {reply_match.group(1).strip()}")
        return
    header_match = re.search(r"header:\s+([^\\r\\n]+)", raw)
    if header_match:
        _emit_log("HTTP", f"HDR    | {header_match.group(1).strip()}")
        return


class YtDlpLogBridge:
    def __init__(self, verbosity: int):
        self.verbosity = verbosity
        self._seen: Set[str] = set()

    def debug(self, msg: str):
        _log_ytdlp_http_line(msg, self.verbosity)
        if self.verbosity < 3:
            return
        text = msg.strip()
        if not text:
            return
        # Emit compact deep-trace lines at -vvv to clearly differentiate from -vv.
        keep_markers = (
            "Extracting URL:",
            "Downloading webpage",
            "Downloading android",
            "Downloading web ",
            "Downloading player",
            "Downloading m3u8",
            "Downloading API JSON",
            "Downloading item ",
            "Remote component challenge solver",
            "challenge solving failed",
            "SABR streaming",
            "PO Token",
        )
        if any(marker in text for marker in keep_markers):
            debug_log(self.verbosity, f"no-api yt-dlp trace: {text}", level=3)
        return

    def warning(self, msg: str):
        if self.verbosity < 2:
            return
        text = msg.strip()
        if not text or text in self._seen:
            return
        self._seen.add(text)
        if self.verbosity == 2 and "SABR streaming" in text:
            return
        debug_log(self.verbosity, f"no-api yt-dlp warning: {text}", level=2)

    def error(self, msg: str):
        if self.verbosity >= 2:
            debug_log(self.verbosity, f"no-api yt-dlp error: {msg}", level=2)


def log_result_rows(verbosity: int, rows: List[dict], context: str):
    progress_add_found(len(rows))
    if verbosity < 1:
        return
    _emit_log("DEBUG", f"{context}: {len(rows)} result(s)", level=1)
    for idx, item in enumerate(rows, start=1):
        vid = item.get("id", {}).get("videoId", "?")
        snip = item.get("snippet", {})
        title = snip.get("title", "")
        creator = snip.get("channelTitle", "")
        _emit_log(
            "DEBUG",
            f"{idx:02d}. id={vid} | creator={creator!r} | title={title!r}",
            level=1,
        )


def build_ytdlp_js_runtimes_config() -> dict:
    # Prefer explicit binary paths when available. This avoids issues on systems
    # where node is installed as nodejs.
    runtimes = [
        ("deno", ["deno"]),
        ("node", ["node", "nodejs"]),
        ("quickjs", ["qjs", "quickjs"]),
        ("bun", ["bun"]),
    ]
    config: dict = {}
    for runtime_name, candidates in runtimes:
        for bin_name in candidates:
            path = shutil.which(bin_name)
            if path:
                config[runtime_name] = {"path": path}
                break
    return config


def parse_key_to_positions(key: str) -> List[int]:
    key = key.strip().lower()
    if not key:
        raise ValueError("Key cannot be empty.")

    positions: List[int] = []
    for ch in key:
        if ch in string.hexdigits.lower():
            positions.append(int(ch, 16))
        else:
            raise ValueError("Key must contain only hex/digits characters.")
    return positions


def format_encode_input_error(message: str) -> str:
    if "Suggested key change for character" not in message:
        return f"Input error: {message}"

    step = re.search(r"step (\d+)", message)
    base_pos = re.search(r"base position (\d+)", message)
    suggested = re.search(r"use position (\d+)", message)
    observed = re.search(r"Observed positions: (.+?)\.", message)
    example = re.search(r"Example video: '(.+)' \(([^)]+)\)\.", message)
    char_match = re.search(r"character '(.+?)'", message)

    step_text = step.group(1) if step else "?"
    base_text = base_pos.group(1) if base_pos else "?"
    suggested_text = suggested.group(1) if suggested else "?"
    observed_text = observed.group(1) if observed else "(none)"
    char_text = char_match.group(1) if char_match else "?"

    lines = [
        "Input error: unable to strictly encode a character after full fallback search.",
        f"Step: {step_text}",
        f"Character: {char_text!r}",
        f"Current key position: {base_text}",
        f"Suggested key position: {suggested_text}",
        f"Observed viable positions: {observed_text}",
    ]
    if example:
        lines.append(f"Example candidate: {example.group(1)!r} ({example.group(2)})")
    lines.append(
        "Hint: this run supports adaptive slot recomputation; re-run encode and use the printed Effective Key for decode."
    )
    return "\n".join(lines)


def normalize_message(msg: str) -> str:
    lowered = msg.lower()
    cleaned = []
    for ch in lowered:
        if ch in SUPPORTED_CHARS:
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    return "".join(cleaned)


def validate_message_for_technique(message: str, technique: str):
    if technique != "videoid":
        return
    for ch in message:
        if ch.isalnum() or ch == " ":
            continue
        raise ValueError(
            "Technique 'videoid' supports only letters, digits, and spaces."
        )


def normalize_topic_phrase(value: str) -> str:
    return " ".join(value.lower().strip().split())


def tokenize_topic_text(text: str) -> List[str]:
    raw = [t.lower() for t in re.findall(r"[a-zA-Z0-9]+", text)]
    return [t for t in raw if t not in STOPWORDS and len(t) > 2 and not t.isdigit()]


def extract_phrase_candidates(
    text: str,
    topic_tokens: Set[str],
    min_n: int = 2,
    max_n: int = 4,
) -> List[str]:
    tokens = tokenize_topic_text(text)
    if not tokens:
        return []

    phrases: List[str] = []
    for n in range(min_n, max_n + 1):
        for i in range(0, len(tokens) - n + 1):
            window = tokens[i : i + n]
            if any(tok in topic_tokens for tok in window):
                phrases.append(" ".join(window))
                continue
            expanded = tokens[max(0, i - 1) : min(len(tokens), i + n + 1)]
            if any(tok in topic_tokens for tok in expanded):
                phrases.append(" ".join(window))
    return phrases


def _score_topic_phrase(
    phrase: str,
    topic_tokens: Set[str],
    phrase_counts: dict[str, int],
) -> float:
    tokens = set(tokenize_topic_text(phrase))
    overlap = len(tokens.intersection(topic_tokens))
    coverage = overlap / max(len(topic_tokens), 1)
    freq = phrase_counts.get(phrase, 1)
    length_bonus = 1.0 if 2 <= len(tokens) <= 4 else 0.2
    return (coverage * 4.0) + (overlap * 1.5) + (freq * 0.35) + length_bonus


def expand_topic_variants(
    topic: Optional[str],
    seed_titles: Optional[List[str]] = None,
    seed_creators: Optional[List[str]] = None,
    max_variants: int = MAX_TOPIC_VARIANTS,
) -> List[str]:
    base = normalize_topic_phrase(topic or "")
    if not base:
        return []
    topic_tokens = set(tokenize_topic_text(base))
    if not topic_tokens:
        topic_tokens = set(base.split())

    out: List[str] = []
    seen: Set[str] = set()

    def add(value: str):
        v = normalize_topic_phrase(value)
        if not v or v in seen:
            return
        seen.add(v)
        out.append(v)

    add(base)

    # Dynamic variants mined and ranked from observed seed titles/creators.
    phrase_counts: dict[str, int] = {}
    for src in (seed_titles or []):
        for phrase in extract_phrase_candidates(src, topic_tokens):
            phrase_counts[phrase] = phrase_counts.get(phrase, 0) + 1
    for src in (seed_creators or []):
        for phrase in extract_phrase_candidates(src, topic_tokens):
            phrase_counts[phrase] = phrase_counts.get(phrase, 0) + 1

    ranked = sorted(
        phrase_counts.keys(),
        key=lambda p: _score_topic_phrase(p, topic_tokens, phrase_counts),
        reverse=True,
    )
    for phrase in ranked:
        add(phrase)
        if len(out) >= max_variants:
            break

    # Soft pivots: short topical anchors discovered from base tokens.
    for tok in list(topic_tokens)[:3]:
        add(tok)
        add(f"{base} {tok}")
        if len(out) >= max_variants:
            break

    return out[:max_variants]


def extract_playlist_id(value: str) -> str:
    if "list=" in value:
        match = re.search(r"[?&]list=([A-Za-z0-9_-]+)", value)
        if match:
            return match.group(1)
    return value.strip()


def ensure_playlist_url(value: str) -> str:
    value = value.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    playlist_id = extract_playlist_id(value)
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def build_related_queries(topic: Optional[str]) -> List[str]:
    clean_topic = (topic or "").strip()
    if not clean_topic:
        return ["videos"]

    topic_tokens = [t for t in re.split(r"\s+", clean_topic) if t]
    token_subset = topic_tokens[:3]
    joined_subset = " ".join(token_subset)
    related_suffixes = [
        "",
        "tutorial",
        "explained",
        "beginner",
        "course",
        "guide",
        "overview",
        "interview",
        "news",
        "talk",
    ]

    queries: List[str] = []
    seen: Set[str] = set()

    def add(q: str):
        q = " ".join(q.split()).strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)

    add(f"{clean_topic}")
    for suffix in related_suffixes:
        add(f"{clean_topic} {suffix}")
    if joined_subset and joined_subset != clean_topic:
        for suffix in related_suffixes:
            add(f"{joined_subset} {suffix}")
    for tok in token_subset:
        add(f"{tok}")

    return queries


def build_discovery_bases(discovery_mode: str, topic: Optional[str]) -> List[str]:
    topic_clean = normalize_topic_phrase(topic or "")
    bases: List[str] = []
    seen: Set[str] = set()

    def add(x: str):
        x = normalize_topic_phrase(x)
        if x and x not in seen:
            seen.add(x)
            bases.append(x)

    if discovery_mode == "topic":
        for t in expand_topic_variants(topic_clean):
            add(t)
        if not bases:
            add(topic_clean or "videos")
        return bases

    if discovery_mode == "trending":
        terms = ["trending", "viral", "popular now", "most viewed"]
    else:
        terms = ["featured", "recommended", "editor picks", "spotlight"]

    for term in terms:
        add(term)
        if topic_clean:
            add(f"{topic_clean} {term}")
            add(f"{term} {topic_clean}")

    return bases or [topic_clean or "videos"]


def jittered_result_count(base: int = DEFAULT_RESULTS_PER_SEARCH) -> int:
    factor = 1.0 + random.uniform(-SEARCH_RESULT_JITTER, SEARCH_RESULT_JITTER)
    value = int(round(base * factor))
    # youtube search.list supports up to 50 maxResults
    return max(1, min(50, value))


def build_followup_queries(topic: Optional[str], seed_title: str, seed_creator: str) -> List[str]:
    queries: List[str] = []
    seen: Set[str] = set()

    def add(q: str):
        q = " ".join(q.split()).strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)

    topic_part = (topic or "").strip()
    title_tokens = [t for t in re.split(r"\s+", seed_title) if t][:6]
    short_title = " ".join(title_tokens)

    add(f"{short_title}")
    if topic_part:
        add(f"{short_title} {topic_part}")
        add(f"{seed_creator} {topic_part}")
    add(f"{seed_creator}")
    return queries


class YouTubeClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.service = build("youtube", "v3", developerKey=api_key)
        self.search_cache: dict[tuple[str, int, int], List[dict]] = {}
        self.related_cache: dict[tuple[str, int], List[dict]] = {}

    def search_videos(
        self,
        query: str,
        max_results: int = 25,
        debug: int = 0,
        page: int = 1,
    ) -> List[dict]:
        cache_key = (query, max_results, page)
        if cache_key in self.search_cache:
            debug_log(
                debug,
                f"API search cache hit: query={query!r} page={page} max_results={max_results}",
                level=2,
            )
            return self.search_cache[cache_key]
        debug_log(
            debug,
            f"API search: query={query!r} max_results={max_results} page={page}",
        )
        page_token = None
        for _ in range(max(page - 1, 0)):
            req = self.service.search().list(
                q=query,
                part="snippet",
                type="video",
                maxResults=max_results,
                safeSearch="none",
                pageToken=page_token,
            )
            http_log_request(debug, req.method, req.uri, req.body)
            resp = req.execute()
            page_token = resp.get("nextPageToken")
            if not page_token:
                return []
        req = self.service.search().list(
            q=query,
            part="snippet",
            type="video",
            maxResults=max_results,
            safeSearch="none",
            pageToken=page_token,
        )
        http_log_request(debug, req.method, req.uri, req.body)
        resp = req.execute()
        items = resp.get("items", [])
        debug_log(debug, f"API search returned {len(items)} results")
        log_result_rows(debug, items, f"API search results for query={query!r} page={page}")
        self.search_cache[cache_key] = items
        return items

    def search_related(
        self,
        video_id: str,
        max_results: int = 5,
        debug: int = 0,
        seed_title: str = "",
        seed_creator: str = "",
        topic: Optional[str] = None,
    ) -> List[dict]:
        cache_key = (video_id, max_results)
        if cache_key in self.related_cache:
            debug_log(
                debug,
                f"API related cache hit: video_id={video_id} max_results={max_results}",
                level=2,
            )
            return self.related_cache[cache_key]
        debug_log(
            debug,
            f"API related search: video_id={video_id} max_results={max_results}",
        )
        try:
            req = self.service.search().list(
                part="snippet",
                type="video",
                relatedToVideoId=video_id,
                maxResults=max_results,
                safeSearch="none",
            )
            http_log_request(debug, req.method, req.uri, req.body)
            resp = req.execute()
            items = resp.get("items", [])
            debug_log(debug, f"API related returned {len(items)} results")
            log_result_rows(debug, items, f"API related results for seed={video_id}")
            self.related_cache[cache_key] = items
            return items
        except TypeError:
            # Some discovery schemas/clients may not expose relatedToVideoId.
            debug_log(
                debug,
                "API relatedToVideoId unavailable; using query-based related fallback.",
            )
            queries = build_followup_queries(topic, seed_title, seed_creator)
            for q in queries:
                results = self.search_videos(q, max_results=max_results, debug=debug)
                if results:
                    self.related_cache[cache_key] = results
                    return results
            self.related_cache[cache_key] = []
            return []

    def get_playlist_items(
        self, playlist_id: str, debug: int = 0
    ) -> List[tuple[str, str, str]]:
        debug_log(debug, f"API playlist fetch: playlist_id={playlist_id}")
        out: List[tuple[str, str, str]] = []
        page_token = None
        while True:
            req = self.service.playlistItems().list(
                part="snippet",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=page_token,
            )
            http_log_request(debug, req.method, req.uri, req.body)
            resp = req.execute()
            debug_log(
                debug,
                f"API playlist page fetched: items={len(resp.get('items', []))} "
                f"next_page={bool(resp.get('nextPageToken'))}",
            )
            for item in resp.get("items", []):
                snippet = item.get("snippet", {})
                resource = snippet.get("resourceId", {})
                vid = resource.get("videoId")
                title = snippet.get("title", "")
                creator = snippet.get("videoOwnerChannelTitle") or snippet.get(
                    "channelTitle", ""
                )
                if vid:
                    out.append((vid, title, creator))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out


class YouTubeNoApiClient:
    def __init__(self):
        try:
            import yt_dlp  # type: ignore
        except Exception as exc:
            raise FileNotFoundError(
                "yt-dlp is not installed. Install it to use --no-api."
            ) from exc
        self.yt_dlp = yt_dlp
        # Cache up to N search entries per query to avoid repeated extractor calls.
        self.query_entries_cache: dict[str, List[dict]] = {}
        self.query_cached_n: dict[str, int] = {}
        self.related_cache: dict[tuple[str, int, str, str, str], List[dict]] = {}

    def _ydl_opts(self, debug: int, extract_flat: bool = False) -> dict:
        opts: dict = {
            "ignoreerrors": True,
            "skip_download": True,
            "quiet": debug < 2,
            "no_warnings": debug < 3,
            "logger": YtDlpLogBridge(debug),
            "extract_flat": "in_playlist" if extract_flat else False,
            "remote_components": ["ejs:github"],
        }
        js_runtimes = build_ytdlp_js_runtimes_config()
        if js_runtimes:
            opts["js_runtimes"] = js_runtimes
        if debug >= 3:
            opts["verbose"] = True
            opts["printtraffic"] = True
        return opts

    def search_videos(
        self,
        query: str,
        max_results: int = 25,
        debug: int = 0,
        page: int = 1,
    ) -> List[dict]:
        needed_n = max_results * max(page, 1)
        cached_n = self.query_cached_n.get(query, 0)
        if cached_n >= needed_n and query in self.query_entries_cache:
            debug_log(
                debug,
                f"no-api search cache hit: query={query!r} page={page} max_results={max_results}",
                level=2,
            )
            entries = self.query_entries_cache[query]
        else:
            fetch_n = needed_n
            search_expr = f"ytsearch{fetch_n}:{query}"
            debug_log(debug, f"no-api search expression: {search_expr!r}", level=2)
            try:
                with self.yt_dlp.YoutubeDL(self._ydl_opts(debug, extract_flat=True)) as ydl:
                    data = ydl.extract_info(search_expr, download=False)
            except Exception as exc:
                msg = str(exc)
                if "No supported JavaScript runtime could be found" in msg:
                    raise ValueError(
                        "Could not search without API: no supported JavaScript runtime "
                        "found for yt-dlp. Install one of: nodejs, deno, quickjs, bun."
                    ) from exc
                raise ValueError(f"Could not search without API: {msg}") from exc

            entries = data.get("entries", []) if isinstance(data, dict) else []
            self.query_entries_cache[query] = entries
            self.query_cached_n[query] = fetch_n
            debug_log(debug, f"no-api search returned {len(entries)} entries")

        start_idx = (max(page, 1) - 1) * max_results
        end_idx = start_idx + max_results
        paged_entries = entries[start_idx:end_idx]
        out = []
        for entry in paged_entries:
            video_id = (entry or {}).get("id")
            title = (entry or {}).get("title") or ""
            creator = (entry or {}).get("channel") or ""
            if not video_id:
                continue
            out.append(
                {
                    "id": {"videoId": video_id},
                    "snippet": {"title": title, "channelTitle": creator},
                }
            )
        log_result_rows(
            debug,
            out,
            f"no-api search results for query={query!r} page={page}",
        )
        return out

    def search_related(
        self,
        video_id: str,
        max_results: int = 5,
        debug: int = 0,
        seed_title: str = "",
        seed_creator: str = "",
        topic: Optional[str] = None,
    ) -> List[dict]:
        cache_key = (
            video_id,
            max_results,
            seed_title,
            seed_creator,
            topic or "",
        )
        if cache_key in self.related_cache:
            debug_log(
                debug,
                f"no-api related cache hit: video_id={video_id} max_results={max_results}",
                level=2,
            )
            return self.related_cache[cache_key]
        # No direct related-videos endpoint without API; emulate by follow-up search.
        queries = build_followup_queries(topic, seed_title, seed_creator)
        for q in queries:
            results = self.search_videos(q, max_results=max_results, debug=debug)
            if results:
                self.related_cache[cache_key] = results
                return results
        self.related_cache[cache_key] = []
        return []


def get_playlist_items_no_api(
    playlist_value: str, debug: int = 0
) -> List[tuple[str, str, str]]:
    playlist_url = ensure_playlist_url(playlist_value)
    try:
        import yt_dlp  # type: ignore
    except Exception as exc:
        raise FileNotFoundError(
            "yt-dlp is not installed. Install it to use decode --no-api."
        ) from exc

    opts: dict = {
        "ignoreerrors": True,
        "skip_download": True,
        "quiet": debug < 2,
        "no_warnings": debug < 3,
        "logger": YtDlpLogBridge(debug),
        "extract_flat": "in_playlist",
        "remote_components": ["ejs:github"],
    }
    js_runtimes = build_ytdlp_js_runtimes_config()
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes
    if debug >= 3:
        opts["verbose"] = True
        opts["printtraffic"] = True

    debug_log(debug, f"no-api playlist expression: {playlist_url!r}", level=2)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(playlist_url, download=False)
    except Exception as exc:
        msg = str(exc)
        if "No supported JavaScript runtime could be found" in msg:
            raise ValueError(
                "Could not read playlist without API: no supported JavaScript runtime "
                "found for yt-dlp. Install one of: nodejs, deno, quickjs, bun."
            ) from exc
        raise ValueError(f"Could not read playlist without API: {msg}") from exc

    items: List[tuple[str, str, str]] = []
    entries = data.get("entries", []) if isinstance(data, dict) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        vid = (entry.get("id") or "").strip()
        title = (entry.get("title") or "").strip()
        creator = (
            entry.get("channel")
            or entry.get("uploader")
            or entry.get("channel_title")
            or ""
        ).strip()
        if vid:
            items.append((vid, title, creator))
    debug_log(debug, f"no-api playlist parsed items={len(items)}")
    return items


class YouTubeWriteClient:
    def __init__(
        self,
        client_secrets_file: str = YOUTUBE_OAUTH_CLIENT_SECRETS,
        token_file: str = YOUTUBE_OAUTH_TOKEN_FILE,
        debug: int = 0,
    ):
        self.client_secrets_file = client_secrets_file
        self.token_file = token_file
        self.debug = debug
        self.service = self._build_service()

    def _build_service(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(
                self.token_file, YOUTUBE_WRITE_SCOPES
            )

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.client_secrets_file):
                    raise FileNotFoundError(
                        f"OAuth client secrets file not found: {self.client_secrets_file}"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.client_secrets_file, YOUTUBE_WRITE_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

        return build("youtube", "v3", credentials=creds)

    def create_playlist(self, name: str, description: str = "") -> str:
        req = self.service.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {"title": name, "description": description},
                "status": {"privacyStatus": "public"},
            },
        )
        http_log_request(self.debug, req.method, req.uri, req.body)
        resp = req.execute()
        return resp["id"]

    def add_video_to_playlist(self, playlist_id: str, video_id: str) -> Tuple[bool, str]:
        req = self.service.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        )
        http_log_request(self.debug, req.method, req.uri, req.body)
        try:
            req.execute()
            return True, ""
        except HttpError as err:
            reason = parse_http_error_reason(err)
            # Some discovered videos are stale/removed or unavailable for playlist insertion.
            if reason in {
                "videoNotFound",
                "videoNotAvailable",
                "privateVideo",
                "forbidden",
            }:
                return False, reason or "unavailable"
            raise

    def add_video_to_playlist_get_item_id(self, playlist_id: str, video_id: str) -> Optional[str]:
        req = self.service.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        )
        http_log_request(self.debug, req.method, req.uri, req.body)
        try:
            resp = req.execute()
            return resp.get("id")
        except HttpError:
            return None

    def remove_playlist_item(self, playlist_item_id: str):
        req = self.service.playlistItems().delete(id=playlist_item_id)
        http_log_request(self.debug, req.method, req.uri, req.body)
        req.execute()


def pick_video_for_char(
    yt,
    ch: str,
    base_pos: int,
    topic: Optional[str] = None,
    discovery_mode: str = "topic",
    technique: str = "title",
    debug: int = 0,
    step_label: str = "",
    used_video_ids_for_char: Optional[Set[str]] = None,
    used_video_ids_global: Optional[Set[str]] = None,
    results_per_search: int = DEFAULT_RESULTS_PER_SEARCH,
    show_progress: bool = False,
    allow_key_shift: bool = True,
) -> PickedVideo:
    query_target = "_" if (ch == " " and technique == "videoid") else ("-" if ch == " " else ch)
    if technique == "videoid":
        target_chars = {"_"} if ch == " " else {ch}
    else:
        target_chars = {"-", " "} if ch == " " else {ch}
    pos_candidates = build_position_candidates_for_technique(base_pos, technique)
    topic_variants = build_discovery_bases(discovery_mode, topic)
    queries: List[str] = []
    seen_query_seed: Set[str] = set()
    for variant in topic_variants or [topic or "videos"]:
        for q in build_related_queries(variant):
            if q not in seen_query_seed:
                seen_query_seed.add(q)
                queries.append(q)
    used_video_ids_for_char = used_video_ids_for_char or set()
    used_video_ids_global = used_video_ids_global or set()
    debug_log(
        debug,
        f"{step_label}Encoding character {ch!r} using {technique!r} text at key "
        f"position {base_pos}. I will try up to {DEFAULT_MAX_SEARCH_CALLS} searches "
        f"across {len(queries)} topic-based query templates. "
        f"Target results/search: ~{results_per_search} (jitter {int(SEARCH_RESULT_JITTER*100)}%).",
    )
    if debug >= 1:
        debug_log(debug, f"{step_label}Topic variants in play: {topic_variants!r}")
    info_log(
        show_progress,
        f"{step_label}I am looking for a strict match for {ch!r} "
        f"at position {base_pos}.",
    )

    pending_queries = list(queries)
    seen_queries: Set[str] = set()
    seen_seed_videos: Set[str] = set()
    search_calls = 0
    query_pages: dict[str, int] = {q: 1 for q in queries}
    # Simulate infinite scroll: same query can be revisited with deeper pages.
    max_pages_per_query = 8
    query_result_targets: dict[str, int] = {q: results_per_search for q in queries}
    observed_counts: Dict[int, int] = {}
    observed_examples: Dict[int, Tuple[str, str]] = {}
    announced_no_match_yet = False
    key_shift_pools: Dict[int, List[tuple]] = {p: [] for p in pos_candidates[1:]}

    def strict_candidates(results: List[dict], query_used: str) -> Dict[int, List[tuple]]:
        pools: Dict[int, List[tuple]] = {p: [] for p in pos_candidates}
        for item in results:
            vid = item["id"]["videoId"]
            title = item["snippet"]["title"]
            creator = item["snippet"].get("channelTitle", "")
            if technique == "title":
                source_text = title
            elif technique == "creator":
                source_text = creator
            else:
                source_text = vid
            source_text_l = source_text.lower()
            if vid in used_video_ids_for_char:
                continue
            if vid in used_video_ids_global:
                continue
            for idx, c in enumerate(source_text_l):
                if c in target_chars:
                    observed_counts[idx] = observed_counts.get(idx, 0) + 1
                    if idx not in observed_examples:
                        observed_examples[idx] = (vid, title)
            for p in pos_candidates:
                if p < len(source_text_l) and source_text_l[p] in target_chars:
                    pools[p].append((vid, title, creator, source_text_l[p], query_used, p))
        return pools

    while pending_queries and search_calls < DEFAULT_MAX_SEARCH_CALLS:
        query = pending_queries.pop(0)
        if query in seen_queries and query_pages.get(query, 1) <= 1:
            continue
        seen_queries.add(query)
        page = query_pages.get(query, 1)
        base_target = query_result_targets.get(query, results_per_search)
        max_results = jittered_result_count(base_target)
        search_calls += 1
        progress_log(
            show_progress,
            f"{step_label}Search {search_calls}/{DEFAULT_MAX_SEARCH_CALLS}: "
            f"topic query page {page}...",
        )
        debug_log(
            debug,
            f"{step_label}Search {search_calls}/{DEFAULT_MAX_SEARCH_CALLS}: "
            f"running topic query {query!r} (page {page}, taking {max_results} "
            f"results) and checking for an exact character match.",
        )
        results = yt.search_videos(
            query, max_results=max_results, debug=debug, page=page
        )
        if not results:
            if not announced_no_match_yet:
                info_log(show_progress, f"{step_label}I haven't found any matches yet.")
                announced_no_match_yet = True
            continue

        strict_pools = strict_candidates(results, query)
        base_pool = strict_pools.get(pos_candidates[0], [])
        if base_pool:
            chosen = random.choice(base_pool)
            chosen_pos = pos_candidates[0]
            progress_log(
                show_progress,
                f"{step_label}Match found at key position {chosen_pos}.",
            )
            info_log(
                show_progress,
                f"{step_label}I've found a match for {ch!r}.",
            )
            if chosen_pos == base_pos and search_calls == 1:
                match_type = "strict"
            elif chosen_pos == base_pos:
                match_type = "fallback"
            else:
                match_type = "key-shift"
            debug_log(
                debug,
                f"{step_label}Found exact match at key position {chosen_pos} "
                f"(base {base_pos}). Selected {chosen[1]!r}. "
                f"Match source: {match_type}. Query used: {query!r}.",
            )
            return PickedVideo(
                video_id=chosen[0],
                title=chosen[1],
                creator=chosen[2],
                intended_char=ch,
                key_pos=chosen[5],
                extracted_char=chosen[3],
                match_type=match_type,
                query_used=chosen[4],
            )
        for p in pos_candidates[1:]:
            pool = strict_pools.get(p, [])
            if pool:
                key_shift_pools[p].extend(pool)

        if page < max_pages_per_query and search_calls < DEFAULT_MAX_SEARCH_CALLS:
            query_pages[query] = page + 1
            grown_target = min(
                50, int(round(base_target * (1.0 + SEARCH_RESULT_JITTER)))
            )
            query_result_targets[query] = max(grown_target, base_target + 1)
            progress_log(
                show_progress,
                f"{step_label}No match yet. Loading more results for current query.",
            )
            info_log(
                show_progress,
                f"{step_label}I couldn't find anything yet, so I'm expanding by loading "
                "subsequent pages and wider results.",
            )
            debug_log(
                debug,
                f"{step_label}No exact match yet. Queuing the next page for the same "
                f"query ({query!r}, page {page + 1}) with a larger result window "
                f"(target {query_result_targets[query]} before jitter).",
            )
            pending_queries.append(query)

        # Expand exploration from current results into related/follow-up searches.
        for seed in results[:5]:
            if search_calls >= DEFAULT_MAX_SEARCH_CALLS:
                break
            seed_vid = seed["id"]["videoId"]
            if seed_vid in seen_seed_videos:
                continue
            seen_seed_videos.add(seed_vid)
            seed_title = seed["snippet"]["title"]
            seed_creator = seed["snippet"].get("channelTitle", "")
            related_max = jittered_result_count(results_per_search)
            search_calls += 1
            progress_log(
                show_progress,
                f"{step_label}Search {search_calls}/{DEFAULT_MAX_SEARCH_CALLS}: "
                "checking related videos from a seed result...",
            )
            info_log(
                show_progress,
                f"{step_label}I couldn't find anything yet, so I'm expanding using "
                "related/recommended videos.",
            )
            debug_log(
                debug,
                f"{step_label}Search {search_calls}/{DEFAULT_MAX_SEARCH_CALLS}: "
                f"expanding from seed video {seed_vid} to find topic-related "
                f"alternatives ({related_max} results).",
            )
            related = yt.search_related(
                seed_vid,
                max_results=related_max,
                debug=debug,
                seed_title=seed_title,
                seed_creator=seed_creator,
                topic=topic,
            )
            strict_related_pools = strict_candidates(related, f"related:{seed_vid}")
            base_pool = strict_related_pools.get(pos_candidates[0], [])
            if base_pool:
                chosen = random.choice(base_pool)
                chosen_pos = pos_candidates[0]
                progress_log(
                    show_progress,
                    f"{step_label}Match found at key position {chosen_pos} via related videos.",
                )
                info_log(
                    show_progress,
                    f"{step_label}I've found a match for {ch!r}.",
                )
                debug_log(
                    debug,
                    f"{step_label}Seed expansion found exact match at key position "
                    f"{chosen_pos} (base {base_pos}). Selected {chosen[1]!r} from "
                    f"seed {seed_vid}.",
                )
                return PickedVideo(
                    video_id=chosen[0],
                    title=chosen[1],
                    creator=chosen[2],
                    intended_char=ch,
                    key_pos=chosen[5],
                    extracted_char=chosen[3],
                    match_type="fallback",
                    query_used=chosen[4],
                )
            for p in pos_candidates[1:]:
                pool = strict_related_pools.get(p, [])
                if pool:
                    key_shift_pools[p].extend(pool)

            for follow in build_followup_queries(
                topic, seed_title, seed_creator
            ):
                if follow not in seen_queries and follow not in pending_queries:
                    query_pages[follow] = 1
                    query_result_targets[follow] = results_per_search
                    debug_log(
                        debug,
                        f"{step_label}Adding new topic-related query based on seed "
                        f"metadata: {follow!r}.",
                        level=2,
                    )
                    pending_queries.append(follow)

            dynamic_topics = expand_topic_variants(
                topic if discovery_mode == "topic" else " ".join([topic or "", discovery_mode]).strip(),
                seed_titles=[seed_title],
                seed_creators=[seed_creator],
                max_variants=MAX_TOPIC_VARIANTS,
            )
            for dyn_topic in dynamic_topics:
                if len(topic_variants) >= MAX_TOPIC_VARIANTS:
                    break
                if dyn_topic not in topic_variants:
                    topic_variants.append(dyn_topic)
                    debug_log(
                        debug,
                        f"{step_label}Discovered related topic variant: {dyn_topic!r}",
                        level=1,
                    )
                    for q in build_related_queries(dyn_topic):
                        if q not in seen_queries and q not in pending_queries:
                            query_pages[q] = 1
                            query_result_targets[q] = results_per_search
                            pending_queries.append(q)

    if allow_key_shift:
        for p in pos_candidates[1:]:
            pool = key_shift_pools.get(p, [])
            if not pool:
                continue
            chosen = random.choice(pool)
            progress_log(
                show_progress,
                f"{step_label}No base-position match found. Using key shift to position {p}.",
            )
            info_log(
                show_progress,
                f"{step_label}All fallback searches were exhausted; applying key shift for {ch!r}.",
            )
            debug_log(
                debug,
                f"{step_label}No exact base-position match found after exhausting fallbacks. "
                f"Applying key-shift from {base_pos} to {p} using {chosen[1]!r}.",
            )
            return PickedVideo(
                video_id=chosen[0],
                title=chosen[1],
                creator=chosen[2],
                intended_char=ch,
                key_pos=chosen[5],
                extracted_char=chosen[3],
                match_type="key-shift",
                query_used=chosen[4],
            )

    debug_log(
        debug,
        f"{step_label}No exact match found for character {ch!r} (base key position "
        f"{base_pos}) after exhausting the search budget.",
    )
    progress_log(
        show_progress,
        f"{step_label}No strict match after {search_calls} searches. Generating key-position suggestion...",
    )
    if observed_counts:
        ranked = sorted(
            observed_counts.items(),
            key=lambda kv: (-kv[1], abs(kv[0] - base_pos)),
        )
        top = ranked[:3]
        suggestion_parts = [f"pos {p} ({cnt} hit(s))" for p, cnt in top]
        best_pos = top[0][0]
        ex_vid, ex_title = observed_examples.get(best_pos, ("?", "?"))
        raise ValueError(
            f"No strict match at base position {base_pos}. Suggested key change for "
            f"character {ch!r}: use position {best_pos}. Observed positions: "
            f"{', '.join(suggestion_parts)}. Example video: {ex_title!r} ({ex_vid})."
        )
    raise ValueError(
        f"No strict match candidates observed for character {ch!r}. "
        "Try broadening topic or increasing search budget."
    )


def encode(
    yt,
    message: str,
    key: str,
    topic: Optional[str],
    discovery_mode: str,
    technique: str,
    debug: int = 0,
    results_per_search: int = DEFAULT_RESULTS_PER_SEARCH,
    show_progress: bool = False,
) -> Tuple[List[PickedVideo], List[int], List[int]]:
    validate_message_for_technique(message, technique)
    normalized = normalize_message(message)
    positions = parse_key_to_positions(key)
    active_key_positions = list(positions)
    debug_log(
        debug,
        f"encode start: original={message!r} normalized={normalized!r} "
        f"len={len(normalized)} key_positions={positions} topic={topic!r} "
        f"discover={discovery_mode} technique={technique} "
        f"results_per_search={results_per_search}",
    )
    picked: List[PickedVideo] = []
    used_video_ids_by_char: Dict[str, Set[str]] = {}
    used_video_ids_global: Set[str] = set()
    effective_positions: List[int] = []
    for i, ch in enumerate(normalized):
        slot = i % len(active_key_positions)
        pos = active_key_positions[slot]
        step_label = f"[{i+1}/{len(normalized)}] "
        progress_log(
            show_progress,
            f"{i+1}/{len(normalized)} encoding {ch!r} "
            f"(target position from your key: {pos})",
        )
        try:
            video = pick_video_for_char(
                yt,
                ch,
                base_pos=pos,
                topic=topic,
                discovery_mode=discovery_mode,
                technique=technique,
                debug=debug,
                step_label=step_label,
                used_video_ids_for_char=used_video_ids_by_char.get(ch, set()),
                used_video_ids_global=used_video_ids_global,
                results_per_search=results_per_search,
                show_progress=show_progress,
                allow_key_shift=True,
            )
        except ValueError as err:
            raise ValueError(
                f"Unable to strictly encode character {ch!r} at step {i+1}. {err}"
            ) from err
        picked.append(video)
        effective_positions.append(video.key_pos)
        if video.match_type == "key-shift":
            active_key_positions[slot] = video.key_pos
        used_video_ids_by_char.setdefault(ch, set()).add(video.video_id)
        used_video_ids_global.add(video.video_id)
        progress_add_indexed(1)
        progress_log(
            show_progress,
            f"{i+1}/{len(normalized)} matched at position {video.key_pos} -> {video.title!r}",
        )
    debug_log(debug, f"encode complete: picked={len(picked)}")
    return picked, effective_positions, active_key_positions


def decode(values: List[str], key: str) -> str:
    positions = parse_key_to_positions(key)
    decoded = []
    for i, value in enumerate(values):
        pos = positions[i % len(positions)]
        value_l = value.lower()
        if pos < len(value_l):
            c = value_l[pos]
            decoded.append(" " if c in {"-", "_"} else c)
        else:
            decoded.append("?")
    return "".join(decoded)


def debug_decode(values: List[str], key: str) -> str:
    positions = parse_key_to_positions(key)
    decoded = []
    print("Decode debug:")
    for i, value in enumerate(values):
        pos = positions[i % len(positions)]
        value_l = value.lower()
        if pos < len(value_l):
            raw = value_l[pos]
            out = " " if raw in {"-", "_"} else raw
        else:
            raw = "?"
            out = "?"
        decoded.append(out)
        print(
            f"{i+1:02d}. pos={pos} raw={raw!r} out={out!r} "
            f"text={value!r}"
        )
    return "".join(decoded)


def print_encode_summary(
    videos: List[PickedVideo],
    message: str,
    topic: Optional[str],
    discovery_mode: str,
    technique: str,
    opsec_level: int,
    original_key: str,
    effective_key_full: str,
    effective_key_mode: str,
    playlist_name: Optional[str],
    playlist_id: Optional[str],
    no_api_search: bool,
    results_per_search: int,
    verbosity: int,
):
    playlist_url = (
        f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else "(not created)"
    )
    print("")
    if playlist_id:
        print("PLAYLIST CREATED")
    else:
        print("ENCODE COMPLETE")
    print("")
    print(f"Playlist URL: {playlist_url}")
    print(f"Playlist Name: {playlist_name or '(none)'}")
    print(f"Message: {message}")
    print(f"Topic: {topic or '(none)'}")
    print(f"Discovery: {discovery_mode}")
    print(f"Technique: {technique}")
    print(f"OPSEC Level: {opsec_level}")
    print(f"Search Mode: {'no-api (yt-dlp)' if no_api_search else 'api'}")
    print(f"Results/Search: ~{results_per_search} (jitter {int(SEARCH_RESULT_JITTER*100)}%)")
    print(f"Original Key: {original_key}")
    print(f"Effective Key: {effective_key_full}")
    print(f"Effective Key Mode: {effective_key_mode}")
    if effective_key_mode == "per-character":
        print("Key Shift: adaptive stream (use full Effective Key exactly as printed)")
    if effective_key_full != original_key:
        print("Key Shift: enabled (use Effective Key to decode)")
    print(f"Videos: {len(videos)}")

    key_shift_count = sum(1 for v in videos if v.match_type == "key-shift")
    fallback_count = sum(1 for v in videos if v.match_type == "fallback")
    print(f"Match Breakdown: strict={len(videos) - key_shift_count - fallback_count}, fallback={fallback_count}, key-shift={key_shift_count}")
    if playlist_id:
        print("")
        print("Decode Command:")
        print(
            f"python3 yously.py decode --playlist "
            f"\"https://www.youtube.com/playlist?list={playlist_id}\" "
            f"--key \"{effective_key_full}\" -t {technique}"
        )
    print("")
    print("Video List:")
    for idx, v in enumerate(videos, start=1):
        video_url = f"https://www.youtube.com/watch?v={v.video_id}"
        char_display = "space" if v.intended_char == " " else v.intended_char
        line = f"- [{_bold(char_display)!s}] {v.title} - {v.creator}"
        if verbosity >= 1:
            line += (
                f" | id={v.video_id} | char={v.intended_char!r} | pos={v.key_pos} "
                f"| match={v.match_type} | query={v.query_used!r} | url={video_url}"
            )
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="yously",
        description="Encode/decode message-like payloads via YouTube metadata.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enc = sub.add_parser("encode", help="Encode message into candidate YouTube videos")
    p_enc.add_argument("-m", "--message", required=True, help="Message to encode")
    p_enc_key_group = p_enc.add_mutually_exclusive_group(required=True)
    p_enc_key_group.add_argument(
        "-k",
        "--key",
        help="Hex positional key (characters 0-9,a-f).",
    )
    p_enc_key_group.add_argument(
        "--otp",
        action="store_true",
        help="Generate a one-time-pad hex key (64 chars) and use it for this encode run.",
    )
    p_enc.add_argument("--topic", help="Optional topic hint (e.g. coding, travel, gaming)")
    p_enc.add_argument(
        "--discover",
        default="topic",
        choices=["topic", "trending", "featured"],
        help="Discovery mode for search expansion.",
    )
    p_enc.add_argument(
        "-t",
        "--technique",
        default="title",
        choices=["title", "creator", "videoid"],
        help="Encode using video title text, creator/channel text, or video ID.",
    )
    p_enc.add_argument(
        "-p",
        "--playlist-name",
        help="If provided, create this YouTube playlist and insert encoded videos.",
    )
    p_enc.add_argument(
        "--no-api",
        action="store_true",
        help="Use yt-dlp for encode search only. Playlist writing still uses OAuth API.",
    )
    p_enc.add_argument(
        "--results-per-search",
        type=int,
        default=DEFAULT_RESULTS_PER_SEARCH,
        help="Approximate number of results to fetch per search step (1-50).",
    )
    p_enc.add_argument(
        "-o",
        "--opsec",
        type=int,
        choices=[0, 1, 2],
        default=0,
        help="OPSEC simulation level: 0 (off), 1 (noise), 2 (noise + browsing + mistake).",
    )
    p_enc.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v debug, -vv diagnostics, -vvv deep trace + HTTP wire logs).",
    )

    p_dec = sub.add_parser("decode", help="Decode from playlist metadata")
    p_dec.add_argument(
        "-k",
        "--key",
        required=True,
        help="Hex positional key (characters 0-9,a-f).",
    )
    p_dec.add_argument(
        "-p",
        "--playlist",
        required=True,
        help="Playlist ID or full URL (must be readable/public)",
    )
    p_dec.add_argument(
        "-t",
        "--technique",
        default="title",
        choices=["title", "creator", "videoid"],
        help="Decode from video title text, creator/channel text, or video ID.",
    )
    p_dec.add_argument(
        "--no-api",
        action="store_true",
        help="Decode by scraping playlist metadata with yt-dlp instead of YouTube API.",
    )
    p_dec.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v debug, -vv diagnostics, -vvv deep trace + HTTP wire logs).",
    )

    args = parser.parse_args()
    verbosity = int(getattr(args, "verbose", 0) or 0)
    needs_api = (args.cmd == "encode" and not args.no_api) or (
        args.cmd == "decode" and not args.no_api
    )
    yt = None
    if needs_api:
        if not YOUTUBE_API_KEY or YOUTUBE_API_KEY == "REPLACE_WITH_YOUR_YOUTUBE_API_KEY":
            print("Set YOUTUBE_API_KEY in yously.py before running.")
            return 2
        yt = YouTubeClient(YOUTUBE_API_KEY)

    try:
        if args.cmd == "encode":
            reset_run_state()
            set_opsec_progress_line(args.opsec > 0)
            if args.opsec > 0:
                opsec_log(True, "Standing by.")
            if args.results_per_search < 1 or args.results_per_search > 50:
                print("Input error: --results-per-search must be between 1 and 50.")
                return 2
            encode_key = otp() if args.otp else args.key
            info_log(True, "Preparing encode configuration.")
            if args.otp:
                info_log(True, "Generated a one-time-pad key for this run.")
            else:
                info_log(True, "Using provided key for this run.")
            encode_client = YouTubeNoApiClient() if args.no_api else yt
            if encode_client is None:
                print("Internal error: encode client not initialized.")
                return 2
            opsec = YouTubeOpSecSimulator(
                level=args.opsec,
                show_progress=True,
                verbosity=verbosity,
                info_fn=opsec_log,
                debug_fn=debug_log,
            )
            info_log(
                True,
                f"Initialized search client ({'no-api/yt-dlp' if args.no_api else 'YouTube API'}).",
            )
            if opsec.enabled():
                info_log(True, f"OPSEC enabled at level {args.opsec}.")
            show_progress = True
            progress_log(
                show_progress,
                "Starting encode run. Building candidate video list...",
            )
            topic_label = repr(args.topic) if args.topic else "(no specific topic)"
            info_log(
                show_progress,
                f"I am searching YouTube for videos related to {topic_label} "
                f"using discovery mode {args.discover!r}.",
            )
            debug_log(
                verbosity,
                f"encode mode: no_api={args.no_api} playlist_write={bool(args.playlist_name)}",
            )
            if opsec.level >= 1:
                opsec.simulate_noise(encode_client, args.topic, args.technique)
            if opsec.level >= 2:
                opsec.simulate_human_browsing(encode_client, args.topic)
            videos, effective_positions, effective_key_positions = encode(
                encode_client,
                args.message,
                encode_key,
                args.topic,
                args.discover,
                args.technique,
                debug=verbosity,
                results_per_search=args.results_per_search,
                show_progress=show_progress,
            )
            if not videos:
                print("No candidates found.")
                return 1

            created_playlist_id = None
            if args.playlist_name:
                progress_log(show_progress, f"Creating playlist {args.playlist_name!r} and adding videos...")
                writer = YouTubeWriteClient(debug=verbosity)
                debug_log(verbosity, f"creating playlist: name={args.playlist_name!r}")
                created_playlist_id = writer.create_playlist(
                    args.playlist_name,
                    description=(
                        f"Encoded by yously | topic={args.topic or 'none'} "
                        f"| technique={args.technique}"
                    ),
                )
                info_log(
                    True,
                    f"Playlist created. URL: https://www.youtube.com/playlist?list={created_playlist_id}",
                )
                debug_log(verbosity, f"created playlist id={created_playlist_id}")
                unavailable_ids: Set[str] = set()
                for idx in range(len(videos)):
                    replacement_attempts = 0
                    while True:
                        v = videos[idx]
                        progress_log(
                            show_progress,
                            f"Writing playlist item {idx+1}/{len(videos)}...",
                        )
                        if opsec.level >= 1:
                            opsec.sleep_with_jitter(base=0.15, jitter=0.35, stage="playlist-insert")
                        info_log(
                            True,
                            f"Adding video {idx+1}/{len(videos)}: {v.title!r}",
                        )
                        debug_log(verbosity, f"adding video to playlist: {v.video_id}")
                        ok, reason = writer.add_video_to_playlist(created_playlist_id, v.video_id)
                        if ok:
                            info_log(True, f"Added successfully ({v.video_id}).")
                            break

                        unavailable_ids.add(v.video_id)
                        replacement_attempts += 1
                        progress_log(
                            show_progress,
                            f"Video unavailable for step {idx+1}; searching replacement...",
                        )
                        info_log(
                            True,
                            f"Could not insert video ({reason}). Searching replacement for step {idx+1}.",
                        )
                        if replacement_attempts > 5:
                            progress_done()
                            raise ValueError(
                                f"Failed to replace unavailable video for step {idx+1} "
                                f"after {replacement_attempts-1} attempts."
                            )

                        used_global = {pv.video_id for j, pv in enumerate(videos) if j != idx}
                        used_global.update(unavailable_ids)
                        used_for_char = {
                            pv.video_id
                            for j, pv in enumerate(videos)
                            if j != idx and pv.intended_char == v.intended_char
                        }
                        replacement = pick_video_for_char(
                            encode_client,
                            ch=v.intended_char,
                            base_pos=v.key_pos,
                            topic=args.topic,
                            discovery_mode=args.discover,
                            technique=args.technique,
                            debug=verbosity,
                            step_label=f"[replace {idx+1}/{len(videos)}] ",
                            used_video_ids_for_char=used_for_char,
                            used_video_ids_global=used_global,
                            results_per_search=args.results_per_search,
                            show_progress=show_progress,
                            allow_key_shift=False,
                        )
                        videos[idx] = replacement
                        info_log(
                            True,
                            f"Replacement selected: {replacement.title!r} ({replacement.video_id}). Retrying insert.",
                        )
                        debug_log(
                            verbosity,
                            f"replacement selected: old={v.video_id} new={replacement.video_id} "
                            f"reason={reason}",
                        )
                if opsec.level >= 2:
                    opsec.maybe_add_remove_mistake(
                        writer,
                        created_playlist_id,
                        encoded_video_ids={v.video_id for v in videos},
                        yt_client=encode_client,
                        topic=args.topic,
                    )
                progress_log(show_progress, "Playlist write completed.")
                info_log(True, "Playlist writing phase completed.")

            per_char_effective_key = positions_to_key(effective_positions)
            try:
                loop_effective_key = merge_effective_positions_into_key(
                    encode_key, effective_positions
                )
                effective_key_full = loop_effective_key
                effective_key_mode = "looped"
            except ValueError:
                effective_key_full = per_char_effective_key
                effective_key_mode = "per-character"
            info_log(True, "Final key reconciliation completed.")
            progress_done()

            print_encode_summary(
                videos=videos,
                message=args.message,
                topic=args.topic,
                discovery_mode=args.discover,
                technique=args.technique,
                opsec_level=args.opsec,
                original_key=encode_key,
                effective_key_full=effective_key_full,
                effective_key_mode=effective_key_mode,
                playlist_name=args.playlist_name,
                playlist_id=created_playlist_id,
                no_api_search=args.no_api,
                results_per_search=args.results_per_search,
                verbosity=verbosity,
            )
            return 0

        if args.cmd == "decode":
            reset_run_state()
            set_opsec_progress_line(False)
            show_progress = True
            progress_log(show_progress, "Starting decode run. Fetching playlist metadata...")
            info_log(
                True,
                f"Starting decode from {'no-api/yt-dlp' if args.no_api else 'YouTube API'} source.",
            )
            debug_log(
                verbosity,
                f"decode mode: no_api={args.no_api} technique={args.technique}",
            )
            if args.no_api:
                info_log(True, "Reading playlist metadata without YouTube Data API.")
                items = get_playlist_items_no_api(args.playlist, debug=verbosity)
            else:
                if yt is None:
                    print("Internal error: YouTube API client not initialized.")
                    return 2
                playlist_id = extract_playlist_id(args.playlist)
                info_log(True, f"Reading playlist items for playlist ID {playlist_id}.")
                items = yt.get_playlist_items(playlist_id, debug=verbosity)
            if not items:
                print("No playlist items found (check playlist ID/URL and visibility).")
                return 1
            debug_log(verbosity, f"decode fetched items={len(items)}")
            info_log(True, f"Fetched {len(items)} playlist items. Applying decode key.")

            values = []
            for vid, title, creator in items:
                if args.technique == "title":
                    values.append(title)
                elif args.technique == "creator":
                    values.append(creator)
                else:
                    values.append(vid)
            debug_log(verbosity, f"decode values prepared: count={len(values)}")
            msg = debug_decode(values, args.key) if verbosity >= 1 else decode(values, args.key)
            progress_log(show_progress, "Decode complete.")
            info_log(True, "Decoded message generated.")
            progress_done()
            print(msg)
            return 0

    except HttpError as err:
        progress_done()
        if verbosity >= 2:
            traceback.print_exc()
        print(f"YouTube API error: {err}")
        return 2
    except ValueError as err:
        progress_done()
        if verbosity >= 2:
            traceback.print_exc()
        print(format_encode_input_error(str(err)))
        return 2
    except FileNotFoundError as err:
        progress_done()
        if verbosity >= 2:
            traceback.print_exc()
        print(f"Setup error: {err}")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
