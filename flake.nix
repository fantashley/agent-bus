{
  description = "agent-bus: a local message bus for Claude Code, Codex, Antigravity and Copilot CLI";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: rec {
        agent-bus = pkgs.callPackage ./nix/package.nix { };
        # For plain git users: no jj on the wrapped PATH or in the tests.
        agent-bus-git = pkgs.callPackage ./nix/package.nix { withJujutsu = false; };
        default = agent-bus;
      });

      homeManagerModules.default = import ./nix/hm-module.nix self;

      checks = forAll (pkgs: {
        package = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        # The package build never evaluates the home-manager module, so at least
        # make sure it parses (importing it does).
        package-git = self.packages.${pkgs.stdenv.hostPlatform.system}.agent-bus-git;
        hm-module = builtins.seq (import ./nix/hm-module.nix) (pkgs.runCommand "hm-module-parses" { } "touch $out");
        # The plugin layouts the module installs, built the same way (nix/plugins.nix), with
        # the skill in each of them.
        plugins =
          let
            built = import ./nix/plugins.nix {
              inherit pkgs;
              inherit (pkgs) lib;
              package = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
              profileBin = "/profile/bin/agent-bus";
              claudeHooks = true;
              antigravityHooks = true;
            };
            bare = import ./nix/plugins.nix {
              inherit pkgs;
              inherit (pkgs) lib;
              package = pkgs.writeShellScriptBin "agent-bus" "" // { version = "0"; };
              profileBin = "/profile/bin/agent-bus";
            };
          in
          pkgs.runCommand "agent-bus-plugins-layout" { } ''
            set -eu
            check() { [ -f "$1" ] || { echo "missing: $1" >&2; exit 1; }; }
            skill() {
              check "$1/SKILL.md"
              head -n 1 "$1/SKILL.md" | grep -qx -- '---'
              grep -qx "name: ''${2:-agent-bus}" "$1/SKILL.md"
              grep -q '^description: ' "$1/SKILL.md"
            }
            prskill() { skill "$1" agent-bus-pr-review; check "$1/pr_review.py"; }
            c=${built.claudeMarketplace}
            check $c/.claude-plugin/marketplace.json
            check $c/plugins/agent-bus/.claude-plugin/plugin.json
            check $c/plugins/agent-bus/.mcp.json
            check $c/plugins/agent-bus/hooks/hooks.json
            skill $c/plugins/agent-bus/skills/agent-bus
            prskill $c/plugins/agent-bus/skills/agent-bus-pr-review
            a=${built.antigravityPlugin}
            check $a/plugin.json
            check $a/mcp_config.json
            check $a/hooks.json
            skill $a/skills/agent-bus
            prskill $a/skills/agent-bus-pr-review
            skill ${built.skill}
            prskill ${built.prReviewSkill}
            # A package without its own copy of the skill (an older or custom one) falls back
            # to this tree's.
            skill ${bare.skill}
            skill ${bare.claudeMarketplace}/plugins/agent-bus/skills/agent-bus
            prskill ${bare.prReviewSkill}
            touch $out
          '';
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [ (pkgs.python3.withPackages (ps: [ ps.mcp ps.pytest ps.pytest-xdist ])) pkgs.jujutsu ];
          shellHook = ''export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"'';
        };
      });

      formatter = forAll (pkgs: pkgs.nixpkgs-fmt);
    };
}
