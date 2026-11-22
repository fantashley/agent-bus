{
  description = "agent-bus: a local message bus for Claude Code, Codex and Antigravity";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: rec {
        agent-bus = pkgs.callPackage ./nix/package.nix { };
        default = agent-bus;
      });

      homeManagerModules.default = import ./nix/hm-module.nix self;

      checks = forAll (pkgs: { package = self.packages.${pkgs.system}.default; });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [ (pkgs.python3.withPackages (ps: [ ps.mcp ps.pytest ])) pkgs.jujutsu ];
          shellHook = ''export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"'';
        };
      });

      formatter = forAll (pkgs: pkgs.nixpkgs-fmt);
    };
}
