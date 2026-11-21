# agent-bus

A local message bus that lets **Claude Code**, **Codex** and **Antigravity** talk to each other
directly, instead of Ashley relaying messages between windows. Designed in
[fantashley/dotfiles#131](https://github.com/fantashley/dotfiles/issues/131).

- **One stdio MCP server** (`agent-bus mcp --agent <type>`) registered in every client. The server
  stamps the sender from its launch command, so agents can't impersonate each other.
- **SQLite (WAL) per host** at `$XDG_STATE_HOME/agent-bus/bus.db`, mode `0600`. History is never
  deleted; read state is a per-session, per-thread cursor.
- **On-demand worker, no daemon.** Typed actions (`start_discussion`, `advance_discussion`,
  `request_review`) queue a job and spawn `agent-bus run-job <id>`, which holds an SQLite lease and
  runs the peers headless and read-only (`claude -p --permission-mode plan`,
  `codex exec` with a read-only sandbox, `agy --mode plan --sandbox`), resuming each peer's
  conversation every round.
- **Safety in the server:** peer text (including an agent's discussion question) is returned as
  `trust: "peer_data"` inside visible fences; only the CLI posts `trust: "human"`. Round caps,
  message caps (`THREAD_LIMIT_REACHED` until Ashley runs `resume`), deadlines and job
  de-duplication. Jobs whose worker died are reaped, so a crash never wedges a thread.
- **Headless peers can't use tools that act.** Claude runs with `--strict-mcp-config` and only
  Read/Grep/Glob/web tools; Codex has every live MCP server disabled (and the turn fails if they
  can't be listed); Antigravity has no per-session switch, so its turns are refused while it has any
  MCP server besides agent-bus configured, unless `peers.antigravity.allow_mcp` is set. Any
  agent-bus MCP server a peer launches sees `AGENT_BUS_PEER` and refuses every call. The repo
  allowlist decides which repos requests may target and where peers run; reads are bounded by
  each CLI's read-only mode, not by the allowlist.

## Discussions

```sh
agent-bus ask "SQLite or a JSON file for a small CLI?" --with claude,codex,antigravity --wait
```

1. Round 1 is blind: each participant proposes without seeing the others (`P1`, `P2`, …).
2. Later rounds run peers in turn; each votes `agree`/`object` on a proposal id or revises one.
   Authoring a proposal counts as agreeing with it.
3. Consensus = every participant agrees on the same proposal. The bus posts a `final` message and
   marks the thread `resolved`.
4. Out of rounds or past the deadline → `needs_human`, with who backs what and who objects.

An agent can start one from its own chat with the `start_discussion` tool; it takes part with
`send` + `wait`. If its turn ends, it's marked absent, and if the peers agree without it the thread
waits in its inbox for its vote.

## Reviews

`request_review(reviewer, repo, base_commit_id, head_commit_id, …)` pins full commit ids. The worker
computes `jj diff --from base --to head --git`, the reviewer returns a structured verdict, and it
lands as a `review_verdict` in the requester's inbox.

For the next pass, request again with `prev_head_commit_id` set to the head that reviewer last saw.
That continues the **same thread** and **resumes the reviewer's own conversation**, so it remembers
its earlier findings; it gets the interdiff plus anything you posted on the thread since (e.g. a
`send` explaining how each finding was addressed). If the resume fails it falls back to a fresh
session with the full diff and thread.

## Watching and stepping in

```sh
agent-bus threads                 # list open threads
agent-bus tail t-1a2b3c [--peer codex|--events]
agent-bus reply t-1a2b3c "Prefer the simpler option" [--to codex]
agent-bus cancel t-1a2b3c
agent-bus resume t-1a2b3c --rounds 2
agent-bus watch t-1a2b3c          # inside Rex: open viewer panes
agent-bus stream [t-1a2b3c …]     # one line per verdict / final / failure, as each arrives
```

`stream` is meant for notifications: each event is one stdout line, so a harness that turns
lines into alerts (or a plain `while read`) hears about each reviewer as it finishes.

`tail` emits [OSC 7501 program status](https://www.superlogical.com/rex/docs/build/program-status)
(`working`, `done`, `blocked/question` on `needs_human`), so Rex shows discussion state on any
client, local or attached to a remote host. Viewer panes are output-only; nothing is ever typed
into a block. Everything works without Rex.

## Install (Home Manager)

```nix
# flake inputs
agent-bus = { url = "github:fantashley/agent-bus"; inputs.nixpkgs.follows = "nixpkgs"; };

# home-manager modules
imports = [ agent-bus.homeManagerModules.default ];
local.agentBus = {
  enable = true;
  repos = [ "~/dotfiles" "~/src" ];   # what peers may read
};
```

This installs the CLI, writes `~/.config/agent-bus/config.json`, and registers the MCP server:

- **Claude Code:** a local plugin from a directory marketplace in the store, enabled through
  dotfiles' `local.claude.extraKnownMarketplaces`/`enabledPlugins` when those options exist
  (otherwise use `config.local.agentBus.claudeMarketplace`).
- **Codex:** `codex mcp add agent-bus …` at activation, skipped when the entry already matches.
- **Antigravity:** a plugin linked at `~/.gemini/config/plugins/agent-bus` (on by default when
  `local.antigravity.enable` is set).

Add [`docs/AGENTS-section.md`](docs/AGENTS-section.md) to the shared global agent rules.

## Development

```sh
nix develop -c pytest      # tests use fake peer CLIs that speak each client's stream format
nix build                  # package (runs the tests)
```

## Not in v1

Interactive peer panes and `rex send` wake-ups (v1.1, gated on a verified input-ready signal);
cross-host discussions, per-client fixed-hint hooks, writable participants in separate
`jj workspace`s (v1.5).
