"""Turn a localised Pokemon card name into the English one shown on stream.

Japanese, Korean and Chinese prints carry the localised species name, and the
CardSight catalog reports it as-is. The lookup table in `data/pokemon_names.json`
is generated from PokeAPI's official localisations (see
`tools/fetch_pokemon_names.py`), so nothing here is guessed — a name is either
translated from that table or reported as untranslated.

Card names are usually a species name plus a Latin suffix ("リザードンex"), so the
matcher replaces the longest species substring and keeps the rest.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "pokemon_names.json"

NON_LATIN_RANGES = (
    (0x3040, 0x30FF),  # hiragana, katakana
    (0x31F0, 0x31FF),  # katakana phonetic extensions
    (0x3400, 0x4DBF),  # CJK unified ideographs extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0x1100, 0x11FF),  # hangul jamo
    (0x3130, 0x318F),  # hangul compatibility jamo
    (0xAC00, 0xD7AF),  # hangul syllables
    (0xFF66, 0xFF9F),  # halfwidth katakana
)


def has_non_latin(text: str | None) -> bool:
    """True when the text contains CJK or Hangul characters."""
    if not text:
        return False
    return any(
        any(low <= ord(ch) <= high for low, high in NON_LATIN_RANGES) for ch in text
    )


@lru_cache(maxsize=1)
def _table() -> tuple[dict[str, str], tuple[str, ...]]:
    """The lookup, plus its keys ordered longest-first.

    Longest-first matters: several species names are prefixes of others, and
    matching the short one would mistranslate the card.
    """
    try:
        payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        names: dict[str, str] = payload.get("names", {})
    except Exception:
        names = {}
    return names, tuple(sorted(names, key=len, reverse=True))


@dataclass(frozen=True)
class Translation:
    name: str
    # True when the whole name resolved; False when untranslatable text was dropped.
    complete: bool
    translated: bool


def translate_card_name(raw: str | None) -> Translation:
    """Best-effort English rendering of a card name.

    Returns the original untouched when it is already Latin script. When part of
    the name cannot be resolved, the untranslatable run is dropped and `complete`
    is False, so the caller can still prompt an operator to finish the job.
    """
    if not raw:
        return Translation(name="", complete=False, translated=False)

    text = unicodedata.normalize("NFKC", raw).strip()
    if not has_non_latin(text):
        return Translation(name=text, complete=True, translated=False)

    names, ordered = _table()

    exact = names.get(text)
    if exact:
        return Translation(name=exact, complete=True, translated=True)

    for localised in ordered:
        if localised in text:
            english = names[localised]
            merged = text.replace(localised, f" {english} ")
            # Anything still non-Latin is decoration we cannot resolve
            # ("かがやく" / Radiant, trainer possessives, and so on).
            leftover = "".join(
                ch for ch in merged if any(low <= ord(ch) <= high for low, high in NON_LATIN_RANGES)
            )
            for ch in leftover:
                merged = merged.replace(ch, " ")
            merged = re.sub(r"\s+", " ", merged).strip()
            return Translation(name=merged, complete=not leftover, translated=True)

    return Translation(name=text, complete=False, translated=False)
