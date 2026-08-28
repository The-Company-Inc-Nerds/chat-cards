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

      # Boots the module in mock mode and drives the real HTTP surface, so a
      # broken unit or a bad hardening flag fails here rather than mid-stream.
      module = pkgs.testers.runNixOSTest {
        name = "chat-cards-module";
        nodes.machine = {...}: {
          imports = [self.nixosModules.default];
          services.chat-cards = {
            enable = true;
            mock = true;
            port = 8787;
          };
        };
        testScript = ''
          machine.wait_for_unit("chat-cards.service")
          machine.wait_for_open_port(8787)

          machine.succeed("curl -sf http://localhost:8787/healthz | grep '\"ok\": true'")
          machine.succeed("curl -sf http://localhost:8787/overlay | grep -q 'reel__track'")
          machine.succeed("curl -sf http://localhost:8787/scan | grep -q 'viewfinder'")

          # Open a pack, then confirm it survives a restart of the unit.
          machine.succeed(
              "curl -sf -X POST http://localhost:8787/api/pack "
              "-H 'Content-Type: application/json' -d '{\"size\": 5, \"winner\": \"@tester\"}'"
          )
          machine.succeed("curl -sf http://localhost:8787/api/state | grep -q '@tester'")

          machine.systemctl("restart chat-cards.service")
          machine.wait_for_open_port(8787)
          machine.succeed("curl -sf http://localhost:8787/api/state | grep -q '@tester'")

          # State must land in the StateDirectory, not somewhere ProtectSystem allows by luck.
          machine.succeed("test -f /var/lib/chat-cards/session.json")
        '';
      };
    };
  };
}
