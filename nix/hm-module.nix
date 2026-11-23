self:
{ config, lib, pkgs, options, ... }:

# Home Manager module for agent-bus.
#
# Installs the CLI, writes ~/.config/agent-bus/config.json, and registers the
# MCP server with each client without touching their mutable config files:
#
# - Claude Code: a local plugin served from a directory marketplace in the
#   store. When the importing config defines `local.claude` options,
#   it is enabled through local.claude.extraKnownMarketplaces/enabledPlugins;
#   otherwise wire `local.agentBus.claudeMarketplace` up yourself.
# - Codex: `agent-bus setup-codex` at activation, which runs `codex mcp add`
#   and sets env_vars on that one entry (so REX_* reach the server), skipped
#   when the entry already matches; the rest of ~/.codex/config.toml is left alone.
#   env_vars forwards the environment of whatever starts the server: under
#   Codex's shared app-server daemon (the TUI's default) that's the daemon, so
#   Rex-aware interactive Codex sessions need `codex --no-daemon`. This module
#   doesn't change how codex is launched or its features (see README).
# - Antigravity: a plugin directory linked at ~/.gemini/config/plugins/agent-bus.

let
  cfg = config.local.agentBus;
  bin = lib.getExe cfg.package;
  json = pkgs.formats.json { };

  mcpEntry = agent: {
    command = bin;
    args = [ "mcp" "--agent" agent ];
  };

  claudeMarketplace = pkgs.runCommand "agent-bus-claude-marketplace" { } (''
    mkdir -p $out/.claude-plugin $out/plugins/agent-bus/.claude-plugin
    cp ${json.generate "marketplace.json" {
      name = "agent-bus";
      owner.name = "agent-bus";
      metadata.description = "Local agent-bus MCP server for Claude Code";
      plugins = [{
        name = "agent-bus";
        source = "./plugins/agent-bus";
        description = "Talk to Codex and Antigravity over the local agent bus";
      }];
    }} $out/.claude-plugin/marketplace.json
    cp ${json.generate "plugin.json" {
      name = "agent-bus";
      version = cfg.package.version;
      description = "Talk to Codex and Antigravity over the local agent bus";
    }} $out/plugins/agent-bus/.claude-plugin/plugin.json
    cp ${json.generate "mcp.json" { mcpServers.agent-bus = mcpEntry "claude"; }} \
      $out/plugins/agent-bus/.mcp.json
  '' + lib.optionalString cfg.claude.hooks.enable ''
    mkdir -p $out/plugins/agent-bus/hooks
    cp ${claudeHooks} $out/plugins/agent-bus/hooks/hooks.json
  '');

  # Fixed-hint hooks (v1.5): a constant "you have agent-bus mail" line at prompt submit,
  # and at most one Stop continuation per human turn. See README "Hooks".
  hookCmd = event: {
    type = "command";
    command = "${bin} hook --client claude --event ${event}";
    timeout = 5;
  };
  claudeHooks = json.generate "hooks.json" {
    description = "agent-bus: fixed unread-mail hint at turn boundaries";
    hooks = {
      UserPromptSubmit = [{ hooks = [ (hookCmd "prompt") ]; }];
      Stop = [{ hooks = [ (hookCmd "stop") ]; }];
    };
  };

  # Antigravity has no prompt-submit event: PreInvocation runs before every model call,
  # and `agent-bus hook` acts only on the first one of a turn (invocationNum 0). Native
  # plugins must keep hooks.json at the plugin root; agy doesn't look in hooks/.
  agyHookCmd = event: {
    type = "command";
    command = "${bin} hook --client antigravity --event ${event}";
    timeout = 5;
  };
  antigravityHooks = json.generate "hooks.json" {
    agent-bus = {
      PreInvocation = [ (agyHookCmd "prompt") ];
      Stop = [ (agyHookCmd "stop") ];
    };
  };

  antigravityPlugin = pkgs.runCommand "agent-bus-antigravity-plugin" { } (''
    mkdir -p $out
    cp ${json.generate "plugin.json" {
      name = "agent-bus";
      version = cfg.package.version;
      description = "Talk to Claude Code and Codex over the local agent bus";
    }} $out/plugin.json
    cp ${json.generate "mcp_config.json" { mcpServers.agent-bus = mcpEntry "antigravity"; }} \
      $out/mcp_config.json
  '' + lib.optionalString cfg.antigravity.hooks.enable ''
    cp ${antigravityHooks} $out/hooks.json
  '');

  hasDotfilesClaude = options ? local && options.local ? claude
    && options.local.claude ? enabledPlugins
    && options.local.claude ? extraKnownMarketplaces;
  # Whether a harness's client is in use: listed itself, or running a listed participant.
  listed = harness: cfg.agents == null || lib.elem harness cfg.agents
    || lib.any (name: lib.elem name cfg.agents)
      (lib.attrNames (lib.filterAttrs (_: p: p.harness == harness) cfg.participants));
  # Repo entries with web_search left null are written as plain paths.
  repos = map (r: if lib.isString r then r
    else if r.web_search == null then r.path else { inherit (r) path web_search; }) cfg.repos;
  # Only the keys that are set, so the harness's defaults apply otherwise.
  participants = lib.mapAttrs (_: v: lib.filterAttrs (_: x: x != null) v) cfg.participants;
  # Only the keys that are set, so the CLI's defaults apply otherwise.
  peerSettings = lib.filterAttrs (_: v: v != { })
    (lib.mapAttrs (_: v: lib.filterAttrs (_: x: x != null) v) cfg.peerSettings);
  defaultAntigravity = (options ? local && options.local ? antigravity)
    && config.local.antigravity.enable or false;
in
{
  options.local.agentBus = {
    enable = lib.mkEnableOption "agent-bus, the local message bus between coding agents";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.${if cfg.jujutsu.enable then "default" else "agent-bus-git"};
      defaultText = lib.literalExpression ''agent-bus.packages.''${system}.''${if jujutsu.enable then "default" else "agent-bus-git"}'';
      description = "The agent-bus package.";
    };

    jujutsu.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Bundle jj with agent-bus, for reviews in jj repositories. Plain git repos never
        use it; turn it off if you don't use jj.
      '';
    };

    repos = lib.mkOption {
      type = lib.types.listOf (lib.types.either lib.types.str (lib.types.submodule {
        options = {
          path = lib.mkOption {
            type = lib.types.str;
            description = "The repository or parent directory.";
          };
          web_search = lib.mkOption {
            type = lib.types.nullOr lib.types.bool;
            default = null;
            example = false;
            description = ''
              false: peers working in this repo may not search the web, whatever their own
              `web_search` says. An entry can only turn it off (null or true: as configured
              per agent), so any entry covering a repo that sets false wins.
            '';
          };
        };
      }));
      default = [ ];
      example = lib.literalExpression ''[ "~/dotfiles" "~/src" { path = "~/work"; web_search = false; } ]'';
      description = ''
        Repositories (or parent directories) that discussions and reviews may
        target. A request naming a repo outside this list is refused, and peers
        run with that repo as their working directory. This is not a read
        sandbox: what a peer can read is governed by its own CLI's read-only
        mode (plan mode / read-only sandbox). An entry can also be
        `{ path; web_search = false; }` to keep peers off the web in that repo.
      '';
    };

    agents = lib.mkOption {
      type = lib.types.nullOr (lib.types.listOf lib.types.str);
      default = null;
      example = [ "claude" "codex" "codex-mini" ];
      description = ''
        The participants available on this machine: the built-in claude, codex and
        antigravity, and any names from `participants`. Discussions and reviews default to
        these, naming any other fails up front, and registration is only done for the
        clients they run on. Null detects them at runtime (a CLI found on PATH counts).
      '';
    };

    participants = lib.mkOption {
      type = lib.types.attrsOf (lib.types.submodule {
        options = {
          harness = lib.mkOption {
            type = lib.types.enum [ "claude" "codex" "antigravity" ];
            description = "The CLI harness that runs this participant.";
          };
          model = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = "Model for its headless turns (null: the harness's peerSettings, else the CLI's default).";
          };
          effort = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = "Reasoning effort for its headless turns (null: as for model).";
          };
          web_search = lib.mkOption {
            type = lib.types.nullOr lib.types.bool;
            default = null;
            description = "Whether its headless turns may search the web (null: as for its harness).";
          };
          allow_mcp = lib.mkOption {
            type = lib.types.nullOr lib.types.bool;
            default = null;
            description = ''
              Antigravity only: run its headless turns even though agent-bus can't check its MCP
              servers (needed for a participant with its own `command`; null: as for its harness).
            '';
          };
          account = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "work";
            description = ''
              A label for the account it signs in with, when that isn't its harness's default
              (say, a `command` wrapper that uses another login). Usage limits are tracked per
              harness and account, so one account running out doesn't stop the other.
            '';
          };
          codex_home = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "~/.codex-work";
            description = ''
              Codex only: the `CODEX_HOME` its headless turns run with, an absolute path or one
              starting with `~` (null: the harness's `peers.codex.codex_home`, else the worker's
              own). agent-bus also reads usage-limit readings from that home's rollout files, so
              a home other than the harness's needs an `account` of its own.
            '';
          };
          command = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = "The CLI to run (null: the harness's `peers.<harness>.command`, else its default).";
          };
          args = lib.mkOption {
            type = lib.types.nullOr (lib.types.listOf lib.types.str);
            default = null;
            description = "Extra CLI arguments (null: the harness's `peers.<harness>.args`).";
          };
        };
      });
      default = { };
      example = lib.literalExpression ''{ codex-mini = { harness = "codex"; model = "gpt-5-mini"; effort = "low"; }; }'';
      description = ''
        Named participants: separately addressable agents (a sender name, a mailbox, a
        discussion peer or reviewer) run by one of the harnesses with their own settings.
        Settings left null come from the harness's `peerSettings` and `peers.<harness>`
        config. The built-in claude, codex and antigravity are always participants too. An
        interactive client speaks as one with `agent-bus mcp --agent <harness> --participant
        <name>`.
      '';
    };

    peerSettings = lib.mkOption {
      type = lib.types.attrsOf (lib.types.submodule {
        options = {
          model = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "opus";
            description = "Model for this agent's headless turns (null: the CLI's default).";
          };
          effort = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "high";
            description = ''
              Reasoning effort for this agent's headless turns (null: the CLI's default).
              Claude Code and Antigravity: low, medium, high, xhigh, max. Codex: minimal,
              low, medium, high, xhigh. agent-bus rejects other values.
            '';
          };
          web_search = lib.mkOption {
            type = lib.types.nullOr lib.types.bool;
            default = null;
            example = false;
            description = ''
              Whether this agent's headless turns may search the web (null: on). A `repos`
              entry can also turn it off for every agent in that repo.
            '';
          };
        };
      });
      default = { };
      example = lib.literalExpression ''{ codex.effort = "high"; claude.model = "opus"; }'';
      description = ''
        Per-agent model, reasoning effort and web search for the headless peers agent-bus
        runs in discussions and reviews. A discussion or review can override model and effort
        per thread.
      '';
    };

    settings = lib.mkOption {
      type = json.type;
      default = { };
      example = { max_rounds = 3; deadline_minutes = 20; };
      description = ''
        Extra keys for config.json: max_rounds, deadline_minutes,
        turn_timeout_s, initiator_grace_s, quota_cooldown_s, max_messages, max_diff_bytes,
        `peers.<agent>.{command,args}` to override how a peer CLI is run,
        `peers.codex.codex_home` for the CODEX_HOME Codex peers run with, and
        `peers.antigravity.allow_mcp = true` to let Antigravity peers run even
        though its other MCP servers can't be switched off per session.
      '';
    };

    claude.enable = lib.mkOption {
      type = lib.types.bool;
      default = listed "claude";
      defaultText = lib.literalExpression ''agents == null || elem "claude" agents || a listed participant runs on claude'';
      description = "Register agent-bus with Claude Code (as a local plugin).";
    };
    claude.hooks.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Add fixed-hint hooks to the Claude Code plugin. When this session has unread
        agent-bus mail, a prompt gets a constant hint ("Unread messages are available
        for this session. Call the agent-bus inbox tool...") as context, and a turn may
        be kept going once (at most once per human turn) so Claude can read mail that
        arrived during it. The hint never contains another agent's text. Off by default
        because it changes how Claude behaves at turn boundaries.
      '';
    };
    codex.enable = lib.mkOption {
      type = lib.types.bool;
      default = listed "codex";
      defaultText = lib.literalExpression ''agents == null || elem "codex" agents || a listed participant runs on codex'';
      description = "Register agent-bus with Codex (`agent-bus setup-codex`) when codex is on PATH.";
    };
    codex.hooks.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Add fixed-hint hooks to Codex's hooks.json (UserPromptSubmit and Stop) via
        `agent-bus setup-codex`. Points hooks at the stable profile path so Home
        Manager rebuilds keep Codex's persisted hook trust; trust them once in
        Codex's own prompt (never bypasses hook trust). When false, activation
        removes agent-bus's own entries from hooks.json while leaving unrelated
        hooks untouched. Off by default because it changes how Codex behaves at
        turn boundaries.
      '';
    };
    antigravity.enable = lib.mkOption {
      type = lib.types.bool;
      default = if cfg.agents == null then defaultAntigravity else listed "antigravity";
      defaultText = lib.literalExpression ''if agents == null then (config.local.antigravity.enable or false) else (elem "antigravity" agents || a listed participant runs on antigravity)'';
      description = "Link the agent-bus plugin into ~/.gemini/config/plugins.";
    };
    antigravity.hooks.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Add fixed-hint hooks (PreInvocation and Stop) to the Antigravity plugin's
        hooks.json. When this session has unread agent-bus mail, the first model call of
        a turn gets a constant hint as a system message, and a normally finished turn may
        be kept going once so Antigravity can read mail that arrived during it. The hint
        never contains another agent's text. Needs `antigravity.enable` (the plugin
        these hooks ship in). Off by default because it changes how Antigravity behaves
        at turn boundaries.
      '';
    };

    claudeMarketplace = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      default = claudeMarketplace;
      description = "Directory marketplace holding the agent-bus Claude Code plugin.";
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      home.packages = [ cfg.package ];
      xdg.configFile."agent-bus/config.json".source =
        json.generate "agent-bus-config.json" (lib.recursiveUpdate
          ({ inherit repos; }
            // lib.optionalAttrs (cfg.agents != null) { inherit (cfg) agents; }
            // lib.optionalAttrs (peerSettings != { }) { peers = peerSettings; }
            // lib.optionalAttrs (participants != { }) { inherit participants; })
          cfg.settings);
    }

    (lib.mkIf cfg.antigravity.enable {
      home.file.".gemini/config/plugins/agent-bus".source = antigravityPlugin;
    })

    (lib.mkIf cfg.codex.enable {
      # After installPackages so a codex installed in this same generation is
      # found, with the profile on PATH because activation's own PATH is minimal.
      home.activation.agentBusCodex = lib.hm.dag.entryAfter [ "writeBoundary" "installPackages" ] ''
        PATH="${config.home.profileDirectory}/bin:/etc/profiles/per-user/${config.home.username}/bin:$PATH"
        if command -v codex >/dev/null 2>&1; then
          # Adds or fixes only the agent-bus entry, including env_vars so the server
          # can see which Rex block it's in; a no-op when it already matches.
          ${if cfg.codex.hooks.enable then ''
            run ${lib.escapeShellArg bin} setup-codex --bin ${lib.escapeShellArg bin} \
              --hooks --hooks-bin ${lib.escapeShellArg "${config.home.profileDirectory}/bin/agent-bus"}
          '' else ''
            run ${lib.escapeShellArg bin} setup-codex --bin ${lib.escapeShellArg bin}
            if [ -e "''${CODEX_HOME:-$HOME/.codex}/hooks.json" ]; then
              if ! run ${lib.escapeShellArg bin} setup-codex --hooks-only --no-hooks; then
                echo "agent-bus: warning: could not clean up Codex hooks.json; continuing activation" >&2
              fi
            fi
          ''}
        else
          verboseEcho "agent-bus: codex not on PATH; skipping Codex MCP registration"
        fi
      '';
    })

    (lib.mkIf cfg.claude.enable (lib.optionalAttrs hasDotfilesClaude {
      local.claude.extraKnownMarketplaces.agent-bus.source = {
        source = "directory";
        path = "${claudeMarketplace}";
      };
      local.claude.enabledPlugins."agent-bus@agent-bus" = true;
    }))
  ]);
}
