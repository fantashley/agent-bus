self:
{ config, lib, pkgs, options, ... }:

# Home Manager module for agent-bus.
#
# Installs the CLI, writes ~/.config/agent-bus/config.json, and registers the
# MCP server with each client without touching their mutable config files:
#
# - Claude Code: a local plugin served from a directory marketplace linked at a
#   stable path (`local.agentBus.claudeMarketplacePath`), whose commands run the
#   agent-bus in the Home Manager profile. Claude Code remembers a marketplace's
#   path in ~/.claude/plugins/known_marketplaces.json and doesn't follow later
#   settings changes, so a store path there would pin it to one build. When the
#   importing config defines `local.claude` options, it is enabled through
#   local.claude.extraKnownMarketplaces/enabledPlugins; otherwise register the path
#   yourself. Activation moves a registration of an older store path to the stable one.
# - Codex: `agent-bus setup-codex` at activation, which runs `codex mcp add`
#   and sets env_vars on that one entry (so REX_* reach the server), skipped
#   when the entry already matches; the rest of ~/.codex/config.toml is left alone.
#   env_vars forwards the environment of whatever starts the server: under
#   Codex's shared app-server daemon (the TUI's default) that's the daemon, so
#   Rex-aware interactive Codex sessions need `codex --no-daemon`. This module
#   doesn't change how codex is launched or its features (see README).
# - Antigravity: a plugin directory linked at ~/.gemini/config/plugins/agent-bus.
# - The agent-bus skill (skills/agent-bus/SKILL.md, the how-to agents read) ships in the
#   Claude Code and Antigravity plugins, and is linked at ~/.agents/skills/agent-bus, where
#   Codex and Copilot CLI load user skills. The plugins are built by ./plugins.nix.
# - Copilot CLI: `agent-bus setup-copilot` at activation, which adds or fixes only
#   the agent-bus entry in ~/.copilot/mcp-config.json ($COPILOT_HOME), leaving
#   every other server alone; a no-op when it already matches.

let
  cfg = config.local.agentBus;
  bin = lib.getExe cfg.package;
  # The profile's agent-bus, for commands kept in places that outlive a rebuild (the
  # Claude Code plugin, Codex hooks): it always runs the current build.
  profileBin = "${config.home.profileDirectory}/bin/agent-bus";
  claudeMarketplacePath = "${config.xdg.dataHome}/agent-bus/claude-marketplace";
  json = pkgs.formats.json { };

  plugins = import ./plugins.nix {
    inherit lib pkgs profileBin;
    package = cfg.package;
    claudeHooks = cfg.claude.hooks.enable;
    antigravityHooks = cfg.antigravity.hooks.enable;
  };
  inherit (plugins) claudeMarketplace antigravityPlugin;

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
        The participants available on this machine: the built-in claude, codex,
        antigravity and copilot, and any names from `participants`. Discussions and reviews default to
        these, naming any other fails up front, and registration is only done for the
        clients they run on. Null detects them at runtime (a CLI found on PATH counts).
      '';
    };

    participants = lib.mkOption {
      type = lib.types.attrsOf (lib.types.submodule {
        options = {
          harness = lib.mkOption {
            type = lib.types.enum [ "claude" "codex" "antigravity" "copilot" ];
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
          env = lib.mkOption {
            type = lib.types.nullOr (lib.types.attrsOf lib.types.str);
            default = null;
            example = { AWS_PROFILE = "bedrock-work"; };
            description = ''
              Environment variables for its headless turns, on top of its harness's
              `peerSettings.<harness>.env` (variable by variable). Non-secret settings only:
              agent-bus refuses credential-looking names and values. A Claude participant whose
              `CLAUDE_CODE_USE_BEDROCK` or `AWS_PROFILE` differs from its harness's needs an
              `account` of its own. See README "Running peers on Amazon Bedrock".
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
        config. The built-in claude, codex, antigravity and copilot are always participants too. An
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
          env = lib.mkOption {
            type = lib.types.nullOr (lib.types.attrsOf lib.types.str);
            default = null;
            example = { CLAUDE_CODE_USE_BEDROCK = "1"; AWS_PROFILE = "bedrock"; AWS_REGION = "us-east-2"; };
            description = ''
              Environment variables for this agent's headless turns, applied to every turn
              whichever client queued it (null: none beyond the worker's own). For Claude Code
              they're also passed as `--settings` env, so they beat ~/.claude/settings.json.
              Never put credentials here (the file is in the Nix store, and agent-bus refuses
              credential-looking names and values): point at an AWS profile instead.
            '';
          };
        };
      });
      default = { };
      example = lib.literalExpression ''{ codex.effort = "high"; claude.model = "opus"; }'';
      description = ''
        Per-agent model, reasoning effort, web search and environment for the headless peers
        agent-bus runs in discussions and reviews. A discussion or review can override model and effort
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
        review_followup_budget,
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
    copilot.enable = lib.mkOption {
      type = lib.types.bool;
      default = listed "copilot";
      defaultText = lib.literalExpression ''agents == null || elem "copilot" agents || a listed participant runs on copilot'';
      description = ''
        Register agent-bus with GitHub Copilot CLI (`agent-bus setup-copilot`, which edits
        only the agent-bus entry of ~/.copilot/mcp-config.json) when copilot is on PATH.
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

    skill.enable = lib.mkOption {
      type = lib.types.bool;
      default = cfg.codex.enable || cfg.copilot.enable;
      defaultText = lib.literalExpression "codex.enable || copilot.enable";
      description = ''
        Link the agent-bus skill (how to start discussions, request reviews and debriefs, and
        what each error code means) at ~/.agents/skills/agent-bus, where Codex and Copilot CLI
        load user skills. The Claude Code and Antigravity plugins carry it themselves.
      '';
    };

    claudeMarketplace = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      default = claudeMarketplace;
      description = ''
        Directory marketplace holding the agent-bus Claude Code plugin. Register
        `claudeMarketplacePath` rather than this store path, which changes with every build.
      '';
    };
    claudeMarketplacePath = lib.mkOption {
      type = lib.types.str;
      readOnly = true;
      default = claudeMarketplacePath;
      defaultText = lib.literalExpression ''"''${config.xdg.dataHome}/agent-bus/claude-marketplace"'';
      description = ''
        Stable path linked to `claudeMarketplace`, to register with Claude Code as a
        directory marketplace. The link follows each rebuild, so Claude Code picks up a new
        build on its next start or `/reload-plugins` without re-adding the marketplace.
      '';
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

    (lib.mkIf cfg.skill.enable {
      home.file.".agents/skills/agent-bus".source = plugins.skill;
    })

    (lib.mkIf cfg.antigravity.enable {
      home.file.".gemini/config/plugins/agent-bus".source = antigravityPlugin;
    })

    (lib.mkIf cfg.copilot.enable {
      home.activation.agentBusCopilot = lib.hm.dag.entryAfter [ "writeBoundary" "installPackages" ] ''
        PATH="${config.home.profileDirectory}/bin:/etc/profiles/per-user/${config.home.username}/bin:$PATH"
        if command -v copilot >/dev/null 2>&1; then
          run ${lib.escapeShellArg bin} setup-copilot --bin ${lib.escapeShellArg bin}
        else
          verboseEcho "agent-bus: copilot not on PATH; skipping Copilot MCP registration"
        fi
      '';
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
              --hooks --hooks-bin ${lib.escapeShellArg profileBin}
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

    (lib.mkIf cfg.claude.enable {
      xdg.dataFile."agent-bus/claude-marketplace".source = claudeMarketplace;

      # Claude Code adds a marketplace once (from settings.json or `claude plugin marketplace
      # add`) and then keeps its own record of the path, so an install that registered one of
      # this module's store paths, as it did before the stable link, keeps loading that build.
      # Point such a record at the stable path. A no-op once it's there, and other paths (a
      # checkout you registered on purpose) are left alone; when Claude Code hasn't recorded
      # the marketplace yet, it adds it from settings.json by itself.
      # Re-adding is enough: Claude Code runs a directory marketplace's plugin from the
      # marketplace directory, not from its versioned copy in plugins/cache (checked with
      # Claude Code 2.1: `claude plugin list` reports "Read from: <marketplace>/plugins/agent-bus"
      # and `claude mcp list` follows content swapped at the same version).
      home.activation.agentBusClaudeMarketplace = lib.hm.dag.entryAfter [ "writeBoundary" "installPackages" "linkGeneration" ] ''
        PATH="${config.home.profileDirectory}/bin:/etc/profiles/per-user/${config.home.username}/bin:$PATH"
        # Where the claude CLI keeps its plugin registry (CLAUDE_CODE_PLUGIN_CACHE_DIR moves it).
        known="''${CLAUDE_CODE_PLUGIN_CACHE_DIR:-''${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins}/known_marketplaces.json"
        if [ -f "$known" ]; then
          registered=$(${lib.getExe pkgs.jq} -r '."agent-bus".source.path // empty' "$known" 2>/dev/null || true)
          case "$registered" in
            ${builtins.storeDir}/*-agent-bus-claude-marketplace)
              if command -v claude >/dev/null 2>&1; then
                if ! run claude plugin marketplace add ${lib.escapeShellArg claudeMarketplacePath} </dev/null; then
                  warnEcho "agent-bus: could not re-register the Claude Code marketplace; run: claude plugin marketplace add ${claudeMarketplacePath}"
                fi
              else
                warnEcho "agent-bus: claude not on PATH; run: claude plugin marketplace add ${claudeMarketplacePath}"
              fi
              ;;
          esac
        fi
      '';
    })

    (lib.mkIf cfg.claude.enable (lib.optionalAttrs hasDotfilesClaude {
      local.claude.extraKnownMarketplaces.agent-bus.source = {
        source = "directory";
        path = claudeMarketplacePath;
      };
      local.claude.enabledPlugins."agent-bus@agent-bus" = true;
    }))
  ]);
}
