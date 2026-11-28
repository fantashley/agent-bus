{ lib, python3Packages, git, jujutsu, withJujutsu ? true }:

python3Packages.buildPythonApplication {
  pname = "agent-bus";
  version = "0.4.0"; # x-release-please-version
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

  # The PATH the caller gave, before the wrapper's own additions: the MCP restart shim starts
  # each later build's server with it, so that build's wrapper adds its own (#139).
  postFixup = ''
    sed -i '1a export AGENT_BUS_CALLER_PATH="$PATH"' $out/bin/agent-bus
    grep -q AGENT_BUS_CALLER_PATH $out/bin/agent-bus
  '';

  # The agent-bus skill (the how-to agents read), installed with the build it describes so
  # the Home Manager module can ship it to each client (see nix/plugins.nix).
  postInstall = ''
    mkdir -p $out/share/agent-bus
    cp -r skills $out/share/agent-bus/skills
  '';

  # PyYAML: the skill tests parse SKILL.md's frontmatter as real YAML (#161); not a runtime dependency.
  nativeCheckInputs = [ python3Packages.pytestCheckHook python3Packages.pyyaml git ]
    ++ lib.optional withJujutsu jujutsu;
  # The review tests make throwaway git (and jj) repos, which want a user and a writable home.
  preCheck = ''
    export HOME=$(mktemp -d)
    export JJ_USER=test JJ_EMAIL=test@example.com
    export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com
    export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com
  '';

  meta = {
    description = "Local message bus that lets Claude Code, Codex, Antigravity and Copilot CLI talk to each other over MCP";
    mainProgram = "agent-bus";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
