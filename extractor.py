"""Fast, dependency-free extraction of the game name from watch-page HTML.

Priority order:
1. ``ytVideoAttributeViewModelTitle`` element (current known markup).
2. Scoped ``"videoAttributeViewModel": {... "title": "..."}`` JSON blob.
3. Explicit serialized game keys (``gameTitle`` / ``videoGame`` / ``gameName``).
4. ``application/ld+json`` blocks walked for game keys.

Every step is scoped to game-specific markup so we never mistake the
video title, channel name, category ("Gaming"), date or view count for
the game. Anything uncertain returns ``None`` -- never a guess.
"""
from __future__ import annotations

import html as _html
import json as _json
import re as _re
from itertools import islice
from typing import Any, Optional

# Compiled once at import: zero per-call regex compile cost.
_TITLE_CLASS_RE = _re.compile(
    r"ytVideoAttributeViewModelTitle[^>]*>\s*([^<]{1,200}?)\s*</h1",
    _re.IGNORECASE | _re.DOTALL,
)
_ATTR_JSON_RE = _re.compile(
    r'"videoAttributeViewModel"\s*:\s*\{[^}]{0,2000}?"title"\s*:\s*"'
    r"((?:\\.|[^\"\\]){1,200})\"",
    _re.DOTALL,
)
_GAME_KEY_RE = _re.compile(
    r'"(?:gameTitle|videoGame|gameName)"\s*:\s*"((?:\\.|[^"\\]){1,200})"'
)
_LDJSON_RE = _re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    _re.IGNORECASE | _re.DOTALL,
)
_WS_RE = _re.compile(r"\s+")
_YEAR_RE = _re.compile(r"^\d{4}$")
_COUNT_RE = _re.compile(r"\d\s*(views|subscribers|followers)", _re.IGNORECASE)

_GAME_KEYS = ("videoGame", "gameName", "game")


def _plausible(value: str) -> Optional[str]:
    """Validate a candidate; return the cleaned name or ``None``."""
    if not value:
        return None
    text = _WS_RE.sub(" ", _html.unescape(value)).strip()
    if len(text) < 2 or len(text) > 100:
        return None
    if text.lower() == "gaming":  # category, not a game
        return None
    if _YEAR_RE.match(text):  # subtitle year, not a game
        return None
    if _COUNT_RE.search(text):  # view/subscriber counts, not a game
        return None
    return text


def _from_json_string(raw: str) -> Optional[str]:
    try:
        decoded = _json.loads('"' + raw + '"')
    except (ValueError, RecursionError):
        decoded = raw
    if not isinstance(decoded, str):
        return None
    return _plausible(decoded)


def _walk_ldjson(node: Any, depth: int = 0) -> Optional[str]:
    if depth > 4:
        return None
    if isinstance(node, dict):
        for key in _GAME_KEYS:
            value = node.get(key)
            if isinstance(value, str):
                game = _plausible(value)
                if game:
                    return game
            elif isinstance(value, dict) and isinstance(value.get("name"), str):
                game = _plausible(value["name"])
                if game:
                    return game
        for value in node.values():
            if isinstance(value, (dict, list)):
                game = _walk_ldjson(value, depth + 1)
                if game:
                    return game
    elif isinstance(node, list):
        for item in node[:20]:
            if isinstance(item, (dict, list)):
                game = _walk_ldjson(item, depth + 1)
                if game:
                    return game
    return None


def _from_ldjson(page: str) -> Optional[str]:
    for match in islice(_LDJSON_RE.finditer(page), 5):
        try:
            data = _json.loads(match.group(1).strip())
        except (ValueError, RecursionError):
            continue
        game = _walk_ldjson(data)
        if game:
            return game
    return None


def extract_game(page: Any) -> Optional[str]:
    """Extract the game name from watch-page HTML, or ``None``.

    Pure function of the HTML string: no network, no JS, no DOM.
    Each step is gated on a cheap substring check so non-gaming pages
    (the common case) skip regex/JSON work almost entirely.
    """
    if not page or not isinstance(page, str):
        return None

    # 1. Current known markup: <h1 class="...ytVideoAttributeViewModelTitle">.
    if "ytVideoAttributeViewModelTitle" in page:
        match = _TITLE_CLASS_RE.search(page)
        if match:
            game = _plausible(match.group(1))
            if game:
                return game

    # 2. Scoped serialized attribute JSON.
    if "videoAttributeViewModel" in page:
        match = _ATTR_JSON_RE.search(page)
        if match:
            game = _from_json_string(match.group(1))
            if game:
                return game

    # 3. Explicit game keys anywhere in serialized player data.
    if '"gameTitle"' in page or '"videoGame"' in page or '"gameName"' in page:
        match = _GAME_KEY_RE.search(page)
        if match:
            game = _from_json_string(match.group(1))
            if game:
                return game

    # 4. schema.org ld+json fallback.
    if "ld+json" in page:
        return _from_ldjson(page)

    return None
