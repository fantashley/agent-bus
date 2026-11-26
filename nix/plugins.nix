# The agent-bus plugins and skill the Home Manager module installs, kept apart from the
# module so `nix flake check` can build the same layout without evaluating Home Manager.
#
# - claudeMarketplace: a directory marketplace holding the Claude Code plugin (MCP server,
#   optional hooks, and the agent-bus skill under skills/).
# - antigravityPlugin: the Antigravity plugin directory (MCP server, optional hooks, skill).
# - skill: the agent-bus skill directory (SKILL.md), from the package, for clients that load
#   skills from a directory (Codex and Copilot CLI read ~/.agents/skills).
# - prReviewSkill: the agent-bus-pr-review skill (SKILL.md and its pr_review.py helper, #148),
#   shipped the same way.
{ lib, pkgs, package, profileBin, claudeHooks ? false, antigravityHooks ? false }:

let
  bin = lib.getExe package;
  json = pkgs.formats.json { };
  # The package's own copy, so the skill matches the build it describes; a package without
  # one (an older or custom `local.agentBus.package`) gets the copy from this source tree.
  skill = pkgs.runCommand "agent-bus-skill" { } ''
    if [ -f ${package}/share/agent-bus/skills/agent-bus/SKILL.md ]; then
      cp -r ${package}/share/agent-bus/skills/agent-bus $out
    else
      cp -r ${../skills/agent-bus} $out
    fi
  '';
  prReviewSkill = pkgs.runCommand "agent-bus-pr-review-skill" { } ''
    if [ -f ${package}/share/agent-bus/skills/agent-bus-pr-review/SKILL.md ]; then
      cp -r ${package}/share/agent-bus/skills/agent-bus-pr-review $out
    else
      cp -r ${../skills/agent-bus-pr-review} $out
    fi
  '';

  # AGENT_BUS_INSTALLED names the profile's agent-bus, whose link each rebuild moves: the
  # MCP server's restart shim compares its build with that one, even when the client's PATH
  # lacks the profile (#139).
  mcpEntry = agent: {
    command = bin;
    args = [ "mcp" "--agent" agent ];
    env.AGENT_BUS_INSTALLED = profileBin;
  };

  # Fixed-hint hooks (v1.5): a constant "you have agent-bus mail" line at prompt submit,
  # and at most one Stop continuation per human turn. See README "Hooks".
  hookCmd = event: {
    type = "command";
    command = "${lib.escapeShellArg profileBin} hook --client claude --event ${event}";
    timeout = 5;
  };
  claudeHooksFile = json.generate "hooks.json" {
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
  antigravityHooksFile = json.generate "hooks.json" {
    agent-bus = {
      PreInvocation = [ (agyHookCmd "prompt") ];
      Stop = [ (agyHookCmd "stop") ];
    };
  };
in
{
  inherit skill prReviewSkill;

  claudeMarketplace = pkgs.runCommand "agent-bus-claude-marketplace" { } (''
    mkdir -p $out/.claude-plugin $out/plugins/agent-bus/.claude-plugin $out/plugins/agent-bus/skills
    cp ${json.generate "marketplace.json" {
      name = "agent-bus";
      owner.name = "agent-bus";
      metadata.description = "Local agent-bus MCP server for Claude Code";
      plugins = [{
        name = "agent-bus";
        source = "./plugins/agent-bus";
        description = "Talk to Codex, Antigravity and Copilot CLI over the local agent bus";
      }];
    }} $out/.claude-plugin/marketplace.json
    cp ${json.generate "plugin.json" {
      name = "agent-bus";
      version = package.version;
      description = "Talk to Codex, Antigravity and Copilot CLI over the local agent bus";
    }} $out/plugins/agent-bus/.claude-plugin/plugin.json
    # The command is the profile's agent-bus, the same path in every build. `/reload-plugins`
    # only restarts a plugin's MCP server when its entry changes (a new plugin version alone
    # doesn't count), so AGENT_BUS_BUILD names the build to make each build's entry differ.
    # Checked with Claude Code 2.1.331 (#92).
    cp ${json.generate "mcp.json" {
      mcpServers.agent-bus = mcpEntry "claude" // {
        command = profileBin;
        env = { AGENT_BUS_INSTALLED = profileBin; AGENT_BUS_BUILD = "${package}"; };
      };
    }} \
      $out/plugins/agent-bus/.mcp.json
    cp -r ${skill} $out/plugins/agent-bus/skills/agent-bus
    cp -r ${prReviewSkill} $out/plugins/agent-bus/skills/agent-bus-pr-review
  '' + lib.optionalString claudeHooks ''
    mkdir -p $out/plugins/agent-bus/hooks
    cp ${claudeHooksFile} $out/plugins/agent-bus/hooks/hooks.json
  '');

  antigravityPlugin = pkgs.runCommand "agent-bus-antigravity-plugin" { } (''
    mkdir -p $out/skills
    cp ${json.generate "plugin.json" {
      name = "agent-bus";
      version = package.version;
      description = "Talk to Claude Code and Codex over the local agent bus";
    }} $out/plugin.json
    cp ${json.generate "mcp_config.json" { mcpServers.agent-bus = mcpEntry "antigravity"; }} \
      $out/mcp_config.json
    cp -r ${skill} $out/skills/agent-bus
    cp -r ${prReviewSkill} $out/skills/agent-bus-pr-review
  '' + lib.optionalString antigravityHooks ''
    cp ${antigravityHooksFile} $out/hooks.json
  '');
}
