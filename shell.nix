{
  pkgs,
  vibe,
}: let
  # python3 with the app's single runtime dependency. Named here so `serve` and
  # the interactive shell cannot drift onto different interpreters.
  pythonEnv = pkgs.python3.withPackages (ps: [ps.aiohttp]);

  # ---------------------------------------------------------------------------
  # serve — run the app straight from ./src, no build step.
  #
  # An inline --key is written to a 0600 temp file and handed over as
  # --api-key-file, so the long-lived server process has a clean argv. Note this
  # does NOT hide the key: `serve` itself stays in the process table with the key
  # in its own argv, and the shell records it in history. On a single-user box
  # that is fine; where it is not, use --key-file or --key-stdin.
  #
  # With no key at all it falls back to --mock rather than starting something
  # that would fail on the first scan.
  # ---------------------------------------------------------------------------
  serve = pkgs.writeShellScriptBin "serve" ''
    set -euo pipefail

    usage() {
      cat <<'USAGE'
    serve — run the chat-cards server from ./src

      serve                        mock mode: canned data, no key, nothing billed
      serve --key KEY              run live with an API key
      serve --key-file PATH        run live with a key read from a file
      serve --key-stdin            run live with a key read from stdin
      serve --mock                 force mock mode even if a key is present

    Without a key, $CHAT_CARDS_API_KEY or $CARDSIGHTAI_API_KEY is used if set;
    failing that, serve starts in mock mode.

    A key passed with --key lands in your shell history and is visible in `ps`
    for as long as serve runs. That is usually fine on your own machine; if it
    is not, prefer a file or stdin:

      serve --key-file /run/secrets/cardsight-api-key
      pass show cardsight/api-key | serve --key-stdin

    Any other arguments are passed through to the server, e.g.

      serve --key KEY --port 9000 --segment auto
      serve --pricing-statistic latest
    USAGE
    }

    # Locate the repo without depending on git — the tree may not be a git repo yet.
    root="$PWD"
    while [ "$root" != "/" ] && [ ! -d "$root/src/chat_cards" ]; do
      root="$(dirname "$root")"
    done
    if [ ! -d "$root/src/chat_cards" ]; then
      echo "serve: run this from inside the chat-cards repo" >&2
      exit 1
    fi

    key=""
    key_file=""
    key_source=""
    force_mock=0
    args=()

    while [ $# -gt 0 ]; do
      case "$1" in
        --key) key="''${2:-}"; key_source="the command line (visible in ps)"; shift 2 ;;
        --key=*) key="''${1#*=}"; key_source="the command line (visible in ps)"; shift ;;
        --key-file) key_file="''${2:-}"; shift 2 ;;
        --key-file=*) key_file="''${1#*=}"; shift ;;
        --key-stdin) read -r key; key_source="stdin"; shift ;;
        --mock) force_mock=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) args+=("$1"); shift ;;
      esac
    done

    if [ "$force_mock" -eq 0 ] && [ -z "$key" ] && [ -z "$key_file" ]; then
      key="''${CHAT_CARDS_API_KEY:-''${CARDSIGHTAI_API_KEY:-}}"
      [ -n "$key" ] && key_source="the environment"
    fi

    mode=""
    if [ "$force_mock" -eq 1 ]; then
      args+=(--mock)
      mode="mock (forced)"
    elif [ -n "$key_file" ]; then
      if [ ! -r "$key_file" ]; then
        echo "serve: cannot read key file: $key_file" >&2
        exit 1
      fi
      args+=(--api-key-file "$key_file")
      mode="live, key from $key_file"
    elif [ -n "$key" ]; then
      # 0600 before the key is written, so it is never briefly world-readable.
      tmp_key="$(mktemp)"
      chmod 600 "$tmp_key"
      trap 'rm -f "$tmp_key"' EXIT
      printf '%s' "$key" > "$tmp_key"
      args+=(--api-key-file "$tmp_key")
      mode="live, key from ''${key_source:-the environment}"
    else
      args+=(--mock)
      mode="mock (no key given)"
    fi

    # Report the port the server will actually use, not the default.
    port=8787
    prev=""
    for a in "''${args[@]}"; do
      case "$a" in
        --port=*) port="''${a#*=}" ;;
        *) [ "$prev" = "--port" ] && port="$a" ;;
      esac
      prev="$a"
    done

    lan="$(${pkgs.iproute2}/bin/ip -4 route get 1.1.1.1 2>/dev/null | ${pkgs.gnugrep}/bin/grep -oP 'src \K\S+' || true)"

    # Fixed-width labels rather than trailing annotations: a LAN address is
    # wider than "localhost", so aligning on the right never lines up.
    echo "chat-cards — $mode"
    echo "  scanner (phone)  http://''${lan:-localhost}:$port/scan"
    echo "  overlay (OBS)    http://localhost:$port/overlay"
    echo ""

    cd "$root"
    # Not exec: the EXIT trap has to fire to shred the temp key file.
    PYTHONPATH="$root/src" ${pythonEnv}/bin/python3 -m chat_cards "''${args[@]}"
  '';

  # ---------------------------------------------------------------------------
  # gcommit — the signed-commit workflow.
  #
  # Commits and tags in this repo are cryptographically *signed* by a human key.
  # An assistant (Claude Code) cannot produce that signature, so it never runs
  # `git commit` itself. Instead it writes the proposed message to the gitignored
  # scratchpad `GIT_COMMIT_MSG`, and a human runs `gcommit` to review, sign, and
  # commit it (and optionally cut a signed tag). See CLAUDE.md.
  # ---------------------------------------------------------------------------
  gcommit = pkgs.writeShellScriptBin "gcommit" ''
    msg_file="GIT_COMMIT_MSG"

    if [[ ! -f "$msg_file" ]] || [[ ! -s "$msg_file" ]]; then
      echo "Error: $msg_file is missing or empty. Nothing to commit." >&2
      exit 1
    fi

    echo ""
    echo "=== Commit message (from $msg_file) ==="
    cat "$msg_file"
    echo "========================================"
    echo ""
    read -r -p "Commit with this message? [y/N] " gc_confirm
    if [[ "$gc_confirm" != "y" && "$gc_confirm" != "Y" ]]; then
      echo "Aborted — $msg_file left unchanged."
      exit 0
    fi

    # -S forces a signature regardless of commit.gpgsign, so a commit through
    # gcommit is always signed (fails loudly if no signing key is configured).
    git commit -S -F "$msg_file"
    gc_exit=$?
    if [[ $gc_exit -ne 0 ]]; then
      echo "Commit failed (exit $gc_exit). $msg_file left unchanged." >&2
      exit $gc_exit
    fi

    echo ""
    read -r -p "Tag this commit? [y/N] " gc_do_tag
    if [[ "$gc_do_tag" == "y" || "$gc_do_tag" == "Y" ]]; then
      read -r -p "Tag name (e.g. v1.2.0): " gc_tag_name
      if [[ -z "$gc_tag_name" ]]; then
        echo "No tag name given — skipping tag."
      else
        read -r -p "Tag annotation (leave blank to reuse commit message): " gc_tag_msg
        if [[ -z "$gc_tag_msg" ]]; then
          git tag -s "$gc_tag_name" -F "$msg_file"
        else
          git tag -s "$gc_tag_name" -m "$gc_tag_msg"
        fi
      fi
    fi

    # Clear the scratchpad so it is not accidentally reused
    > "$msg_file"
    echo ""
    echo "$msg_file cleared. Ready for the next commit."
  '';

  # build-wiki — render docs/ into wiki pages WITHOUT cloning/pushing (preview).
  # Defaults --out to ./wiki-build/ when the caller gives no --out; forwards any
  # extra args. Detects both --out PATH and --out=PATH so the default is never
  # injected as a conflicting second --out.
  build-wiki = pkgs.writeShellScriptBin "build-wiki" ''
    root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
      echo "build-wiki: not inside a git repository" >&2
      exit 1
    }
    has_out=0
    for arg in "$@"; do
      case "$arg" in
        --out | --out=*) has_out=1 ;;
      esac
    done
    if [ "$has_out" -eq 1 ]; then
      python3 "$root/tools/build_wiki.py" "$@"
    else
      python3 "$root/tools/build_wiki.py" --out "$root/wiki-build" "$@"
    fi
  '';

  # publish-wiki — build docs/ into the GitHub wiki repo and push.
  publish-wiki = pkgs.writeShellScriptBin "publish-wiki" ''
    set -euo pipefail
    root="$(git rev-parse --show-toplevel)"
    src="$root/docs"
    builder="$root/tools/build_wiki.py"
    # Wiki repo URL: arg 1 overrides; otherwise <origin> with any trailing slash
    # and a single .git stripped, then .wiki.git appended — correct whether or
    # not origin carried the .git suffix (a naive s/\.git$/…/ would no-op on a
    # .git-less origin and target the MAIN repo).
    if [ -n "''${1:-}" ]; then
      remote="$1"
    else
      origin="$(git -C "$root" remote get-url origin)"
      remote="$(printf '%s' "$origin" | sed -E 's#/+$##; s#\.git$##').wiki.git"
      if [ "$remote" = "$origin" ]; then
        echo "publish-wiki: refusing to run — derived wiki URL equals origin ($origin)" >&2
        exit 1
      fi
    fi

    if [ ! -f "$builder" ]; then
      echo "publish-wiki: builder not found at $builder" >&2
      exit 1
    fi
    if [ ! -d "$src" ] || ! ls "$src"/*.md >/dev/null 2>&1; then
      echo "publish-wiki: no docs pages found in $src" >&2
      exit 1
    fi

    clone="$(mktemp -d)"
    trap 'rm -rf "$clone"' EXIT
    echo "Cloning $remote ..."
    git clone --quiet "$remote" "$clone"

    # Rebuild the flat wiki from docs/: flatten nested pages, rewrite relative
    # links to wiki slugs, regenerate _Sidebar.md/_Footer.md, and validate every
    # internal link + anchor (a broken one aborts here via set -e).
    echo "Building wiki pages from docs/ ..."
    rm -f "$clone"/*.md
    python3 "$builder" --out "$clone"

    cd "$clone"
    git add -A
    if git diff --cached --quiet; then
      echo "Wiki already up to date — nothing to publish."
      exit 0
    fi
    echo "Publishing changes:"
    git diff --cached --stat
    git commit -q -m "Sync wiki from docs/"
    git push
    echo "Published wiki to $remote"
  '';
in
  pkgs.mkShell {
    packages = [
      vibe
      pkgs.git
      pkgs.gh
      pythonEnv
      serve
      gcommit
      build-wiki
      publish-wiki
    ];

    shellHook = ''
      echo "vibe-shell — Claude Code dev environment"
      echo "  vibe          start Claude Code (antlers-pinned: opus[1m], subscription-first)"
      echo "  gcommit       review + sign + commit GIT_COMMIT_MSG (then optional signed tag)"
      echo "  build-wiki    preview docs/ -> ./wiki-build"
      echo "  publish-wiki  publish docs/ -> the repo's GitHub wiki"
      echo ""
      echo "chat-cards"
      echo "  serve                  run in mock mode (no key, nothing billed)"
      echo "  serve --key KEY        run live against the CardSight API"
      echo "  serve --help           key-file, port and pass-through options"
    '';
  }
