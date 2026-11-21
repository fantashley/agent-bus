self:
{ config, lib, pkgs, options, ... }:

# Home Manager module for agent-bus.
#
# Installs the CLI, writes ~/.config/agent-bus/config.json, and registers the
# MCP server with each client without touching their mutable config files:
#
# - Claude Code: a local plugin served from a directory marketplace in the
#   store. When the importing config has the dotfiles `local.claude` options,
#   it is enabled through local.claude.extraKnownMarketplaces/enabledPlugins;
#   otherwise wire `local.agentBus.claudeMarketplace` up yourself.
# - Codex: `codex mcp add` at activation, skipped when the entry already
#   matches, so the rest of ~/.codex/config.toml is left alone.
# - Antigravity: a plugin directory linked at ~/.gemini/config/plugins/agent-bus.

let
  cfg = config.local.agentBus;
  bin = lib.getExe cfg.package;
  json = pkgs.formats.json { };

  mcpEntry = agent: {
    command = bin;
    args = [ "mcp" "--agent" agent ];
  };

  claudeMarketplace = pkgs.runCommand "agent-bus-claude-marketplace" { } ''
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
  '';

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

    settings = lib.mkOption {
      type = json.type;
      default = { };
      example = { max_rounds = 3; deadline_minutes = 20; };
      description = ''
        Extra keys for config.json: max_rounds, deadline_minutes,
        turn_timeout_s, initiator_grace_s, max_messages, max_diff_bytes, and
        `peers.<agent>.{command,args}` to override how a peer CLI is run.
      '';
    };

    claude.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Register agent-bus with Claude Code (as a local plugin).";
    };
    codex.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Register agent-bus with Codex (`codex mcp add`) when codex is on PATH.";
    };
    antigravity.enable = lib.mkOption {
      type = lib.types.bool;
      default = defaultAntigravity;
      defaultText = lib.literalExpression "config.local.antigravity.enable or false";
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
        json.generate "agent-bus-config.json" ({ inherit (cfg) repos; } // cfg.settings);
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
          current=$(codex mcp get agent-bus --json 2>/dev/null \
            | ${lib.getExe pkgs.jq} -c '[.transport.command, .transport.args]' 2>/dev/null || true)
          wanted=$(${lib.getExe pkgs.jq} -nc --arg c ${lib.escapeShellArg bin} '[$c, ["mcp","--agent","codex"]]')
          if [ "$current" != "$wanted" ]; then
            if [ -n "$current" ]; then
              run codex mcp remove agent-bus >/dev/null
            fi
            run codex mcp add agent-bus -- ${lib.escapeShellArg bin} mcp --agent codex >/dev/null
          fi
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
