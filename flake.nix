{
  description = "chat-cards — trading-card pack-opening scanner and Twitch overlay";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    antlers = {
      url = "github:CalamooseLabs/antlers";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = {
    self,
    nixpkgs,
    antlers,
    ...
  }: let
    system = "x86_64-linux";
    pkgs = import nixpkgs {
      inherit system;
      config.allowUnfree = true;
    };
    # The antlers `vibe` launcher: Claude Code pinned to opus[1m], subscription-first.
    # Customise with mkVibeWrapper { model = …; effort = …; permissions = …; }.
    vibe = antlers.lib.${system}.mkVibeWrapper {};
  in {
    packages.${system} = {
      chat-cards = pkgs.callPackage ./nix/package.nix {};
      default = self.packages.${system}.chat-cards;
    };

    # System-independent: the module resolves the package from the importing
    # system's own pkgs, so it works on any platform nixpkgs builds for.
    nixosModules.chat-cards = ./nix/module.nix;
    nixosModules.default = self.nixosModules.chat-cards;

    overlays.default = _final: prev: {
      chat-cards = prev.callPackage ./nix/package.nix {};
    };

    devShells.${system}.default = import ./shell.nix {inherit pkgs vibe;};

    formatter.${system} = pkgs.alejandra;

    checks.${system} = {
      package = self.packages.${system}.chat-cards;

      # The unit tests. They use fakes and loopback sockets only, so they run
      # inside the build sandbox; without this, nothing runs them but a person
      # remembering to.
      unit =
        pkgs.runCommand "chat-cards-unit-tests" {
          nativeBuildInputs = [(pkgs.python3.withPackages (ps: [ps.aiohttp]))];
          src = pkgs.lib.fileset.toSource {
            root = ./.;
            fileset = pkgs.lib.fileset.unions [./src ./tests];
          };
        } ''
          cd "$src"
          PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -s tests
          touch "$out"
        '';

      # Boots the module in mock mode and drives the real HTTP surface, so a
      # broken unit or a bad hardening flag fails here rather than mid-stream.
      # Also the only place the unit's restart behaviour and the resend to
      # multichat are exercised under the unit's own sandbox.
      module = pkgs.testers.runNixOSTest {
        name = "chat-cards-module";
        nodes.machine = {pkgs, ...}: {
          imports = [self.nixosModules.default];
          services.chat-cards = {
            enable = true;
            mock = true;
            port = 8787;
            # Points at the stand-in below rather than a real multichat, so the
            # reporting path is exercised under the unit's own hardening.
            report.url = "http://127.0.0.1:9099/api/turn-report";
          };

          # Stands in for multichat's /api/turn-report: appends each POST body to
          # a file the test can read back. Deliberately unhardened and separate
          # from chat-cards, so what it records is proof the report left the
          # sandboxed unit and crossed a real socket.
          systemd.services.fake-multichat = {
            description = "turn-report sink standing in for multichat";
            wantedBy = ["multi-user.target"];
            before = ["chat-cards.service"];
            serviceConfig.ExecStart = "${pkgs.python3}/bin/python3 ${
              pkgs.writeText "turn-report-sink.py" ''
                import http.server, json

                class Sink(http.server.BaseHTTPRequestHandler):
                    def do_POST(self):
                        body = self.rfile.read(int(self.headers["content-length"]))
                        with open("/var/log/turn-reports.jsonl", "a") as fh:
                            fh.write(json.dumps({
                                "path": self.path,
                                "auth": self.headers.get("authorization"),
                                "body": json.loads(body),
                            }) + "\n")
                        self.send_response(200)
                        self.send_header("content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"ok": true}')

                http.server.HTTPServer(("127.0.0.1", 9099), Sink).serve_forever()
              ''
            }";
          };
        };
        testScript = ''
          import json

          machine.wait_for_unit("chat-cards.service")
          machine.wait_for_unit("fake-multichat.service")
          machine.wait_for_open_port(8787)

          machine.succeed("curl -sf http://localhost:8787/healthz | grep '\"ok\": true'")
          # The credential checks run after the port is open, so give them a beat.
          machine.wait_until_succeeds(
              "curl -sf http://localhost:8787/healthz | grep '\"checks\": \"done\"'"
          )
          # grep -c, not -q: -q quits at the first match, and on a slow builder
          # curl is still writing the rest of the page into the closed pipe,
          # which fails the pipeline although the page was served.
          machine.succeed("curl -sf http://localhost:8787/overlay | grep -c 'reel__track'")
          machine.succeed("curl -sf http://localhost:8787/scan | grep -c 'viewfinder'")

          # Open a pack, then confirm it survives a restart of the unit.
          machine.succeed(
              "curl -sf -X POST http://localhost:8787/api/pack "
              "-H 'Content-Type: application/json' -d '{\"size\": 5, \"winner\": \"@tester\"}'"
          )
          machine.succeed("curl -sf http://localhost:8787/api/state | grep -c '@tester'")

          machine.systemctl("restart chat-cards.service")
          machine.wait_for_open_port(8787)
          machine.succeed("curl -sf http://localhost:8787/api/state | grep -c '@tester'")

          # State must land in the StateDirectory, not somewhere ProtectSystem allows by luck.
          machine.succeed("test -f /var/lib/chat-cards/session.json")

          # The multichat side of the loop: opening a pack must push a report out
          # of the unit, carrying the ref that correlates it back to the turn.
          machine.succeed(
              "curl -sf -X POST http://localhost:8787/api/pack "
              "-H 'Content-Type: application/json' "
              "-d '{\"size\": 5, \"winner\": \"@winner\", \"ref\": \"turn-7\"}'"
          )
          machine.wait_until_succeeds("grep -q 'turn-7' /var/log/turn-reports.jsonl")
          # Select by ref, not by position: the sink also holds the earlier pack's
          # report, and a re-report is sent for every card that lands afterwards.
          report = json.loads(
              machine.succeed("grep 'turn-7' /var/log/turn-reports.jsonl | tail -n1")
          )
          assert report["path"] == "/api/turn-report", report
          assert report["body"]["winner"] == "@winner", report
          assert report["body"]["ref"] == "turn-7", report
          assert report["body"]["packId"], report
          # No report.tokenFile configured, so nothing must be sent as a bearer.
          assert report["auth"] is None, report

          # A pack opened while multichat is away must still reach it: nothing
          # else happens to this pack afterwards, so only the resend can deliver it.
          machine.systemctl("stop fake-multichat.service")
          machine.succeed(
              "curl -sf -X POST http://localhost:8787/api/pack "
              "-H 'Content-Type: application/json' "
              "-d '{\"size\": 5, \"winner\": \"@missed\", \"ref\": \"turn-8\"}'"
          )
          machine.systemctl("start fake-multichat.service")
          machine.wait_until_succeeds("grep -q 'turn-8' /var/log/turn-reports.jsonl")

          # More start jobs in a row than systemd's default limit (5 in 10 s)
          # allows. A unit that can be parked fails one of these outright.
          for _ in range(8):
              machine.succeed("systemctl restart chat-cards.service")
          machine.wait_for_open_port(8787)
          machine.succeed("curl -sf http://localhost:8787/api/state | grep -c '@missed'")

          # Restart=always: the service comes back after a clean exit too.
          pid = "systemctl show -p MainPID --value chat-cards.service"
          before = machine.succeed(pid).strip()
          machine.succeed("systemctl kill --signal=TERM chat-cards.service")
          machine.wait_until_succeeds(
              f'test "$({pid})" -gt 0 && test "$({pid})" != {before} '
              "&& curl -sf http://localhost:8787/healthz"
          )
        '';
      };
    };
  };
}
