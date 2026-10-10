# Offline checks shared by `nix flake check` and the dev shell.
#
# `bun install` needs the network and would pull a different TypeScript
# optional binary per OS, so it cannot be a normal sandboxed build with one
# hash. Instead `mcpNodeModules` is a fixed-output derivation: it downloads
# every tarball named in the Bun lock (all platforms) and extracts them.
# Typecheck and `bun build` then run offline against that tree.
{ stdenv
, python3
, nodejs_22
, bun
, cacert
, clean
}:
let
  pythonEnv = python3.withPackages (ps: [ ps.httpx ]);

  mcpNodeModules = stdenv.mkDerivation {
    pname = "search-skills-mcp-node-modules";
    version = "0";
    nativeBuildInputs = [ python3 cacert ];
    SSL_CERT_FILE = "${cacert}/etc/ssl/certs/ca-bundle.crt";
    outputHashAlgo = "sha256";
    outputHashMode = "recursive";
    outputHash = "sha256-ckDqn0xhBUbn3K2U2W1GdZK2PJ230AldciNRbSBAtVA=";
    dontUnpack = true;
    dontInstall = true;
    dontFixup = true;
    buildPhase = ''
      python3 ${./vendor-bun-lock.py} \
        ${../mcp/exa/bun.lock} \
        ${../mcp/brave/bun.lock} \
        "$out"
    '';
  };

  mcpCheck = name:
    stdenv.mkDerivation {
      pname = "mcp-${name}-check";
      version = "0";
      src = clean ../mcp/${name};
      nativeBuildInputs = [ nodejs_22 bun ];
      dontConfigure = true;
      buildPhase = ''
        runHook preBuild
        cp -a ${mcpNodeModules}/node_modules ./node_modules
        chmod -R u+w node_modules
        # The published tsc shim is "#!/usr/bin/env node", and the sandbox has no /usr/bin/env.
        node ./node_modules/typescript/bin/tsc --noEmit
        bun build index.ts --target=node --outdir build
        node -e "require('fs').chmodSync('build/index.js','755')"
        set +e
        node build/index.js
        status=$?
        set -e
        if [ "$status" -ne 1 ]; then
          echo "mcp/${name}: expected exit 1 when the API key is missing, got $status"
          exit 1
        fi
        runHook postBuild
      '';
      installPhase = ''
        mkdir -p "$out"
        cp build/index.js "$out/index.js"
        echo ok > "$out/ok"
      '';
    };
in
{
  pythonTests = stdenv.mkDerivation {
    pname = "search-skills-python-tests";
    version = "0";
    src = clean ../.;
    nativeBuildInputs = [ pythonEnv ];
    dontConfigure = true;
    buildPhase = ''
      runHook preBuild
      export PYTHONPATH="$PWD/exa/src:$PWD/brave/src"
      python3 -W error::DeprecationWarning -W error::ResourceWarning \
        -m unittest discover -s tests
      runHook postBuild
    '';
    installPhase = ''
      mkdir -p "$out"
      echo ok > "$out/ok"
    '';
  };

  mcpExa = mcpCheck "exa";
  mcpBrave = mcpCheck "brave";
}
