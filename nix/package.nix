{ lib, python3Packages, git, jujutsu, withJujutsu ? true }:

python3Packages.buildPythonApplication {
  pname = "agent-bus";
  version = "0.2.0"; # x-release-please-version
  pyproject = true;
  src = lib.cleanSourceWith {
    src = ../.;
    filter = path: type:
      let base = baseNameOf path; in
      !(lib.elem base [ "result" ".jj" ".git" "__pycache__" ".pytest_cache" ]);
  };

  build-system = [ python3Packages.setuptools ];
  dependencies = [ python3Packages.mcp ];

  # Reviews diff pinned commits with the repo's own tool: jj in a jj repo (when
  # jj is installed), git otherwise. git is always on the wrapped PATH; jj only
  # when withJujutsu (plain-git users can build without it).
  makeWrapperArgs = [ "--suffix" "PATH" ":" (lib.makeBinPath ([ git ] ++ lib.optional withJujutsu jujutsu)) ];

  nativeCheckInputs = [ python3Packages.pytestCheckHook git ] ++ lib.optional withJujutsu jujutsu;
  # The review tests make throwaway git (and jj) repos, which want a user and a writable home.
  preCheck = ''
    export HOME=$(mktemp -d)
    export JJ_USER=test JJ_EMAIL=test@example.com
    export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com
    export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com
  '';

  meta = {
    description = "Local message bus that lets Claude Code, Codex and Antigravity talk to each other over MCP";
    mainProgram = "agent-bus";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
