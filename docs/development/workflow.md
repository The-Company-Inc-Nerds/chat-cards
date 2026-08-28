# Development Workflow

This project runs inside the `vibe-shell` dev environment: a Nix flake dev shell
with [Claude Code](https://claude.com/claude-code), `git`, `gh`, and a few helper
commands. Enter it with `direnv allow` (or `nix develop`).

## Signed commits (`gcommit`)

Every commit and tag in this repo is **cryptographically signed** by a human's
key. An assistant cannot produce that signature, so commits are made in two
steps:

1. The assistant writes the proposed commit message to the gitignored scratchpad
   file `GIT_COMMIT_MSG` (it does **not** run `git commit`).
2. A human runs `gcommit`, which shows the message, asks for confirmation, then
   runs `git commit -S -F GIT_COMMIT_MSG` (signing with the human's key) and
   offers to cut a signed tag (`git tag -s`). On success it clears the scratchpad.

Commit messages are plain, human-style messages — **no** "Generated with Claude"
or "Co-Authored-By" trailers.

See [Home](../index.md) for the docs overview.

## Publishing the wiki

`docs/` is the source of truth. Two commands render it into GitHub-wiki form:

- `build-wiki` — preview only; writes the rendered pages to `./wiki-build/`.
- `publish-wiki` — clones the repo's `….wiki.git`, rebuilds it from `docs/`, and
  pushes.

Both validate every internal link and `#anchor` and abort on a broken one. Edit
the docs, never the wiki directly.
