{ lib, python3Packages, jujutsu }:

python3Packages.buildPythonApplication {
  pname = "agent-bus";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSourceWith {
    src = ../.;
    filter = path: type:
      let base = baseNameOf path; in
      !(lib.elem base [ "result" ".jj" ".git" "__pycache__" ".pytest_cache" ]);
  };

  build-system = [ python3Packages.setuptools ];
  dependencies = [ python3Packages.mcp ];

  nativeCheckInputs = [ python3Packages.pytestCheckHook jujutsu ];
  # The review test makes a throwaway jj repo; jj wants a user and a writable home.
  preCheck = ''
    export HOME=$(mktemp -d)
    export JJ_USER=test JJ_EMAIL=test@example.com
  '';

  meta = {
    description = "Local message bus that lets Claude Code, Codex and Antigravity talk to each other over MCP";
    mainProgram = "agent-bus";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
