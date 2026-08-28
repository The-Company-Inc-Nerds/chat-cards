#!/usr/bin/env python3
"""Regenerate src/chat_cards/data/pokemon_names.json from PokeAPI.

Card names on Japanese, Korean and Chinese prints use the localised species
name. This builds a localised -> English lookup so the overlay can show the
English name without guessing. Run it when a new generation ships:

    python3 tools/fetch_pokemon_names.py

Data comes from PokeAPI (https://pokeapi.co), which sources the official
localisations rather than inferring them.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ENDPOINT = "https://graphql.pokeapi.co/v1beta2"
ENGLISH = 9
# ja-hrkt is the katakana form printed on cards; ja is the kanji form. Korean and
# both Chinese scripts are included because packs come from everywhere.
SOURCE_LANGUAGES = {1: "ja-hrkt", 3: "ko", 4: "zh-hant", 11: "ja", 12: "zh-hans"}

QUERY = """query {
  pokemonspeciesname(where: {language_id: {_in: [1,3,4,9,11,12]}}, limit: 20000) {
    pokemon_species_id
    name
    language_id
  }
}"""

OUT = Path(__file__).resolve().parent.parent / "src" / "chat_cards" / "data" / "pokemon_names.json"


def main() -> None:
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"query": QUERY}).encode(),
        # PokeAPI rejects urllib's default User-Agent with a 403.
        headers={"Content-Type": "application/json", "User-Agent": "chat-cards/0.1 (+name-table generator)"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)

    if "errors" in payload:
        raise SystemExit(f"PokeAPI returned errors: {payload['errors']}")

    by_species: dict[int, dict[int, str]] = {}
    for row in payload["data"]["pokemonspeciesname"]:
        by_species.setdefault(row["pokemon_species_id"], {})[row["language_id"]] = row["name"]

    lookup: dict[str, str] = {}
    skipped = 0
    for species in by_species.values():
        english = species.get(ENGLISH)
        if not english:
            skipped += 1
            continue
        for language_id in SOURCE_LANGUAGES:
            localised = species.get(language_id)
            # Some languages reuse the English string; storing it would be noise.
            if localised and localised != english:
                lookup[localised] = english

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "_source": "https://pokeapi.co — official localised species names",
                "_languages": sorted(SOURCE_LANGUAGES.values()),
                "_species": len(by_species),
                "names": dict(sorted(lookup.items())),
            },
            ensure_ascii=False,
            indent=0,
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT} — {len(lookup)} names across {len(by_species)} species ({skipped} skipped)")


if __name__ == "__main__":
    main()
