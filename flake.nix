{
  description = "Exa and Brave search clients and MCP servers";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "aarch64-darwin" "x86_64-linux" "aarch64-linux" "x86_64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs { inherit system; }));

      searchSkillsCheck = pkgs: pkgs.writeShellScriptBin "search-skills-check" ''
        set -euo pipefail
        root="''${SEARCH_SKILLS_ROOT:-$PWD}"
        cd "$root"
        export PYTHONPATH="$root/exa/src:$root/brave/src''${PYTHONPATH:+:$PYTHONPATH}"
        exec python3 -W error::DeprecationWarning -W error::ResourceWarning \
          -m unittest discover -s tests
      '';

      # Drop vendored installs and Nix/direnv scratch dirs. Tests read
      # .analysis/phase1_signatures.json, so that tree stays in the source.
      repoFilter = path: _type:
        let base = baseNameOf (toString path); in
        !(
          base == "node_modules"
          || base == "build"
          || base == ".direnv"
          || base == "result"
          || nixpkgs.lib.hasPrefix "result-" base
          || base == ".git"
          || base == "__pycache__"
          || nixpkgs.lib.hasSuffix ".pyc" base
        );

      clean = dir: nixpkgs.lib.cleanSourceWith {
        src = dir;
        filter = repoFilter;
      };
    in
    {
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [
              ps.httpx
              ps.hatchling
              ps.build
            ]))
            pkgs.nodejs_22
            pkgs.bun
            (searchSkillsCheck pkgs)
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/exa/src:$PWD/brave/src''${PYTHONPATH:+:}$PYTHONPATH"
          '';
        };
      });

      checks = forAllSystems (pkgs:
        let
          check = pkgs.callPackage ./nix/check.nix { inherit clean; };
        in
        {
          python-tests = check.pythonTests;
          mcp-exa = check.mcpExa;
          mcp-brave = check.mcpBrave;
        });
    };
}
