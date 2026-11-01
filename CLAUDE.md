# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this is

This project was scaffolded from the **`vibe-shell`** template: a Nix flake dev
shell built for working *with* Claude Code. Enter it with `direnv allow` (or
`nix develop`) — that puts `vibe`, `git`, `gh`, `python3`, and the helper
commands below on `PATH`. (`vibe` is the antlers launcher: Claude Code pinned to
opus[1m], subscription-first — run it instead of `claude`.)

```
.
├── flake.nix        # nixpkgs + antlers; devShells.default = ./shell.nix
├── shell.nix        # the dev shell: vibe (antlers) + git + gh + the helpers below
├── nix/             # package.nix + module.nix (the NixOS service)
├── src/chat_cards/  # the app: server, CardSight + pokemontcg clients, state, UI
├── .envrc           # `use flake` (direnv auto-loads the shell)
├── .gitignore       # ignores .direnv, result, GIT_COMMIT_MSG, wiki-build, …
├── CLAUDE.md        # this file
├── docs/            # the wiki source of truth (any *.md, nested freely)
└── tools/
    ├── build_wiki.py  # docs/ -> GitHub-wiki renderer (used by build/publish-wiki)
    ├── live_check.py  # preflight a real CardSight key end to end
    └── fetch_pokemon_names.py  # regenerate the localised -> English name table
```

Build your project on top of this — add sources, a `packages.default`, etc. The
points below are the conventions that make this shell different from a plain one.

## Running the app

`serve` (from the dev shell) runs the server out of `./src` with no build step:
`serve` alone is mock mode — canned identify/pricing data, no API key, nothing
billed — and `serve --key KEY` (or `--key-file`, `--key-stdin`) runs it live.
Extra arguments pass through to the server. `tools/live_check.py --api-key-file
FILE` preflights a real key against the CardSight API.

## Pricing recorded videos

`chat_cards/video.py` (`price-video` in the dev shell, `chat-cards-video` when
installed) finds each card revealed in a pack-opening clip. It looks for still
beats via ffmpeg motion analysis, identifies the stillest frame of each, merges
repeat sightings, prices the cards with the server's PriceCharting → CardSight →
bulk order, and writes a CSV (optionally an SRT) with timestamps and a running
total. `--mock` rehearses offline, and `--at` takes explicit timestamps. The
pure helpers are unit-tested in `tests/test_video.py`.
`docs/guides/video-pricing.md` is the user-facing guide.

## Giveaway integration (multichat)

`chat_cards/reporter.py` lets this app act as the pack-opening side of the
multichat giveaway. multichat opens a pack per drawn winner via the existing
`POST /api/pack` (`{winner, ref, size}` — `ref` is the giveaway turn id, stored
on `Pack.ref` and echoed back, never shown on stream), and the `Reporter`
subscribes to the `Store` event stream and POSTs each pack's cards + running
value back to multichat's `/api/turn-report` so its per-winner ledger stays
complete. Enable with `--report-url URL` + `CHAT_CARDS_REPORT_TOKEN`
(or `--report-token-file`), or `services.chat-cards.report.url` /
`report.tokenFile` on NixOS; it runs in mock mode too, so the whole
giveaway→pack→ledger loop can be rehearsed offline. No token is needed when both
services share a box — multichat accepts unauthenticated reports over loopback.
`docs/guides/giveaway-loop.md` is the operator-facing version. `pack_summary()`
is pure and unit-tested in `tests/` (stdlib `unittest`: `PYTHONPATH=src python -m
unittest discover -s tests`).

## Committing: write the message, never run `git commit`

**Every commit and tag here is cryptographically signed by a human's key.** You
cannot produce that signature, so you do **not** commit. Instead:

1. Stage the changes (`git add …`) as usual.
2. Write the proposed commit message to the file **`GIT_COMMIT_MSG`** at the repo
   root (it is gitignored — a scratchpad). Overwrite it; don't append.
3. Tell the user to run **`gcommit`**. It shows the message, asks for
   confirmation, runs `git commit -S -F GIT_COMMIT_MSG` (signing with their key),
   optionally cuts a signed tag (`git tag -s`), and on success clears the
   scratchpad.

Rules for the message you write into `GIT_COMMIT_MSG`:

- Write a normal, human-style commit message (concise subject line, body as
  needed).
- **Do not** add any "🤖 Generated with Claude Code", "Co-Authored-By: Claude",
  or similar attribution/trailer lines. Commits must read as the human author's
  own signed work.
- Do not run `git commit`, `git commit -S`, `git tag`, or `git push` yourself —
  leave signing and pushing to the human via `gcommit`.

## Documentation → wiki

`docs/` is the single source of truth and renders as plain markdown on
github.com. Two dev-shell commands mirror it into the project's GitHub wiki:

- `build-wiki` — preview; renders `docs/` into `./wiki-build/` (no clone/push).
- `publish-wiki` — clones the repo's `….wiki.git`, rebuilds it from `docs/`, and
  pushes.

`tools/build_wiki.py` auto-discovers every `*.md` under `docs/` (no page list to
maintain), flattens nested pages to flat wiki slugs, rewrites relative links to
those slugs, generates `_Sidebar.md`/`_Footer.md`, and **validates every internal
link and `#anchor`** — a broken link fails the build. So: write docs with normal
relative `.md` links, group pages with subdirectories, and run `build-wiki`
before `publish-wiki` to catch broken links. Never hand-edit the wiki.

## Conventions

- This is a Nix flake. After editing `.nix` files, run `nix flake check`; format
  with `nix fmt` if a formatter is wired up. New files must be `git add`-ed
  before Nix can see them.
- `GIT_COMMIT_MSG` and `wiki-build/` are gitignored — don't commit them.
