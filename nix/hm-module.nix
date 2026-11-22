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

  antigravityPlugin = pkgs.runCommand "agent-bus-antigravity-plugin" { } ''
    mkdir -p $out
    cp ${json.generate "plugin.json" {
      name = "agent-bus";
      version = cfg.package.version;
      description = "Talk to Claude Code and Codex over the local agent bus";
    }} $out/plugin.json
    cp ${json.generate "mcp_config.json" { mcpServers.agent-bus = mcpEntry "antigravity"; }} \
      $out/mcp_config.json
  '';

  hasDotfilesClaude = options ? local && options.local ? claude
    && options.local.claude ? enabledPlugins
    && options.local.claude ? extraKnownMarketplaces;
  listed = agent: cfg.agents == null || lib.elem agent cfg.agents;
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
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "agent-bus.packages.\${system}.default";
      description = "The agent-bus package.";
    };

    repos = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "~/dotfiles" "~/src" ];
      description = ''
        Repositories (or parent directories) that discussions and reviews may
        target. A request naming a repo outside this list is refused, and peers
        run with that repo as their working directory. This is not a read
        sandbox: what a peer can read is governed by its own CLI's read-only
        mode (plan mode / read-only sandbox).
      '';
    };

    agents = lib.mkOption {
      type = lib.types.nullOr (lib.types.listOf (lib.types.enum [ "claude" "codex" "antigravity" ]));
      default = null;
      example = [ "claude" "codex" ];
      description = ''
        The agents available on this machine. Discussions and reviews default to
        these, naming any other agent fails up front, and registration is only done
        for these clients. Null detects them at runtime (a CLI found on PATH counts).
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
        };
      });
      default = { };
      example = lib.literalExpression ''{ codex.effort = "high"; claude.model = "opus"; }'';
      description = ''
        Per-agent model and reasoning effort for the headless peers agent-bus runs in
        discussions and reviews. A discussion or review can override them per thread.
      '';
    };

    settings = lib.mkOption {
      type = json.type;
      default = { };
      example = { max_rounds = 3; deadline_minutes = 20; };
      description = ''
        Extra keys for config.json: max_rounds, deadline_minutes,
        turn_timeout_s, initiator_grace_s, max_messages, max_diff_bytes,
        `peers.<agent>.{command,args}` to override how a peer CLI is run, and
        `peers.antigravity.allow_mcp = true` to let Antigravity peers run even
        though its other MCP servers can't be switched off per session.
      '';
    };

    claude.enable = lib.mkOption {
      type = lib.types.bool;
      default = listed "claude";
      defaultText = lib.literalExpression ''agents == null || elem "claude" agents'';
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
      defaultText = lib.literalExpression ''agents == null || elem "codex" agents'';
      description = "Register agent-bus with Codex (`agent-bus setup-codex`) when codex is on PATH.";
    };
    antigravity.enable = lib.mkOption {
      type = lib.types.bool;
      default = if cfg.agents == null then defaultAntigravity else lib.elem "antigravity" cfg.agents;
      defaultText = lib.literalExpression ''if agents == null then (config.local.antigravity.enable or false) else elem "antigravity" agents'';
      description = "Link the agent-bus plugin into ~/.gemini/config/plugins.";
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
          ({ inherit (cfg) repos; }
            // lib.optionalAttrs (cfg.agents != null) { inherit (cfg) agents; }
            // lib.optionalAttrs (peerSettings != { }) { peers = peerSettings; })
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
          run ${lib.escapeShellArg bin} setup-codex --bin ${lib.escapeShellArg bin}
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
