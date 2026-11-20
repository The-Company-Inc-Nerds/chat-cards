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

## Start-up and state

Start-up never waits on the network, nor on the disk. `_on_startup` in
`server.py` only opens the HTTP sessions, hands the previous run's unfinished
cards to background tasks and starts the reporter; the CardSight key and
PriceCharting token are verified by a background task (`_check_upstreams`),
retried while an upstream is unreachable (`CHECK_RETRY_DELAYS`), and reported
on `/healthz` as `cardsight_auth` / `pricecharting` plus `checks` (`"pending"`
→ `"done"`). Keep anything that can block or fail out of `_on_startup`: the
port is bound only after it returns, and the overlay and `POST /api/pack` must
come up on a box with no internet or a full disk.

Everything under the state directory goes through `write_atomic` in `state.py`
(temp file, fsync, rename). An unreadable `session.json` is moved aside as
`session.json.corrupt-<UTC time>`, never overwritten. A card left `identifying`
by a dead process is settled after the start (`_settle_unfinished`): identified
ones are re-priced once an upstream has answered the start-up check
(`_resume_pricing` — sooner, an offline boot would settle them at $0.00), the
rest are failed like any other bad scan after `INTERRUPTED_SCAN_GRACE`, when the
pages have reconnected to be told — never re-submitted, because identify is the
billed call. `tests/test_state.py` and
`tests/test_server.py` cover this with fakes and loopback sockets only;
`tests/_support.py` holds the stand-in upstream they share.

The NixOS unit (`nix/module.nix`) has its start limit disabled and
`Restart=always` with a short back-off, so it is never parked in `failed`; do
not reintroduce a start limit — the default sits exactly on the unit's restart
interval. It is ordered after `network.target`, not `network-online.target`,
so a boot without an uplink does not hold the overlay back.

## Pricing recorded videos

`chat_cards/video.py` (`price-video` in the dev shell, `chat-cards-video` when
installed) finds each card revealed in a pack-opening clip. It analyses
motion with ffmpeg in a centre crop of the frame, so on-screen captions are
ignored. Each local dip in motion becomes a candidate, because fast-cut shorts
are never fully still. It identifies those frames, merges repeat sightings, prices the cards with the server's PriceCharting → CardSight →
bulk order, and writes a CSV (optionally an SRT) with timestamps and a running
total. `--mock` rehearses offline, and `--at` takes explicit timestamps.
`--reprice CSV` re-reads a hand-corrected CSV and reprices it with
PriceCharting alone, with no video and no CardSight calls. Each row is priced
from its name, number, set and language columns, and
`price_source=manual` rows are kept as they are. `chat_cards/overlay.py` (`price-overlay` in the dev shell,
`chat-cards-overlay` when installed) renders that CSV as a transparent overlay
video. At each card's timestamp, a green "+ $price" hops left or right and fades.
It uses one ffmpeg `drawtext` per card and outputs ProRes 4444 `.mov` or VP9
`.webm`. `--spaced` instead lays the callouts end to end with `--gap` seconds
between them, for cutting by hand, and writes a `.cues.csv`. The pure helpers are unit-tested in `tests/test_video.py` and
`tests/test_overlay.py`.
`docs/guides/video-pricing.md` is the user-facing guide.

## Giveaway integration (multichat)

`chat_cards/reporter.py` lets this app act as the pack-opening side of the
multichat giveaway. multichat opens a pack per drawn winner via the existing
`POST /api/pack` (`{winner, ref, size}` — `ref` is the giveaway turn id, stored
on `Pack.ref` and echoed back, never shown on stream), and the `Reporter`
subscribes to the `Store` event stream and POSTs each pack's cards + running
value back to multichat's `/api/turn-report` so its per-winner ledger stays
complete. A failed POST never blocks a reveal; instead the reporter resends
every pack in the session when it starts, on the first POST that lands after a
failure, on a backed-off timer (`RESYNC_RETRY_DELAYS`) while multichat is
away, and whenever the `Store` had to drop updates it was too far behind to
take (`Subscription.overflowed`) — one POST at a time, live events first
(multichat upserts by pack id, so resending is idempotent). Enable with `--report-url URL` + `CHAT_CARDS_REPORT_TOKEN`
(or `--report-token-file`), or `services.chat-cards.report.url` /
`report.tokenFile` on NixOS; it runs in mock mode too, so the whole
giveaway→pack→ledger loop can be rehearsed offline. No token is needed when both
services share a box — multichat accepts unauthenticated reports over loopback.
`docs/guides/giveaway-loop.md` is the operator-facing version. `pack_summary()`
is pure and unit-tested in `tests/`, and the resend is tested against a loopback
sink that goes down and comes back (stdlib `unittest`: `PYTHONPATH=src python -m
unittest discover -s tests`; `nix flake check` runs the same suite as
`checks.unit`).

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
