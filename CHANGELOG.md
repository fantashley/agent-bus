# Changelog

## [0.3.0](https://github.com/fantashley/agent-bus/compare/v0.2.1...v0.3.0) (2026-11-25)

### Behaviour changes

Check these when upgrading from 0.2.x:

1. **Some checkouts are refused.** A peer turn fails, rather than running, when the checkout's own config could act outside a read-only turn: for Antigravity, the workspace's own hooks, MCP servers or plugins (a non-empty `plugins.json`); for Codex, a `.codex/config.toml` that sets `sqlite_home`, `log_dir` or the `js_repl` node path or modules; for Copilot CLI, repository hooks (unless `disableAllHooks` is set in `.github/copilot/settings.json` or `settings.local.json`) or a non-empty `.github/lsp.json`. Switch them off in the checkout, or review it some other way.
2. **Codex peer turns run no hooks, rules or plugins**, your own included (`--ignore-rules`, and the `hooks` and `plugins` features off).
3. **A single-reviewer review stays `open` after `request_changes`**, waiting for your fix or pushback, as multi-reviewer reviews already did (until the follow-up limit, when it goes to `needs_human`). It used to resolve.
4. **`docs/AGENTS-section.md` is gone**: the `agent-bus` skill and the MCP server's instructions replace it. If you pasted that section into `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md` or similar, delete it after upgrading.
5. **Claude Code peers run with `--permission-mode dontAsk`**, which denies any tool call that isn't allowed instead of prompting.
6. **Antigravity peers no longer pass `--mode plan`**; they keep `--sandbox` and a read-only custom agent.
7. **Review-only reviews** (`review_only=true`) follow someone else's change across their pushes with no follow-up limit (a usage limit can still pause them), and the new `agent-bus-pr-review` skill relays them as a draft GitHub review that you approve before anything is posted.
8. **Provenance and evidence.** Every human action records where it came from, and `history` flags a suspicious one with `!!` lines (posted from an agent's shell, with `AGENT_BUS_ALLOW_HUMAN=1`, or without a terminal). `start_discussion` and `request_review` take `evidence`: read-only facts every peer gets in full.


### Features

* add GitHub Copilot CLI as a harness ([#96](https://github.com/fantashley/agent-bus/issues/96)) ([8b3e028](https://github.com/fantashley/agent-bus/commit/8b3e0289dd46a0b06998fdfe050e393f71e722ba))
* debrief a finished thread's peers in their own resumed sessions ([#97](https://github.com/fantashley/agent-bus/issues/97)) ([adae6ae](https://github.com/fantashley/agent-bus/commit/adae6ae38434efaebe77c68059f2a35c8ecfe2e1))
* finish_review, review-only reviews, debrief after cancel, pre-existing findings ([#149](https://github.com/fantashley/agent-bus/issues/149)) ([1dba65f](https://github.com/fantashley/agent-bus/commit/1dba65f6f0ad7c455274d6fec47f6b676f35b60d))
* human-facing CLI help, threads triage and a guard on human-only commands ([#109](https://github.com/fantashley/agent-bus/issues/109)) ([acab501](https://github.com/fantashley/agent-bus/commit/acab501f8c56f743b939d0073132fc5dd1511c5f))
* let headless peers search the web ([#55](https://github.com/fantashley/agent-bus/issues/55)) ([9a17394](https://github.com/fantashley/agent-bus/commit/9a17394786f129e295d5de33a30da2eabcad6322))
* let reviewers read files exactly as of the pinned head from a read-only snapshot ([#116](https://github.com/fantashley/agent-bus/issues/116)) ([335daaa](https://github.com/fantashley/agent-bus/commit/335daaa7218b8a199aa8d167cde4199886e057e1))
* per-participant codex_home for Codex usage readings ([#78](https://github.com/fantashley/agent-bus/issues/78)) ([44838a9](https://github.com/fantashley/agent-bus/commit/44838a98d1ceef776881770dc5b42d6dc943b0e6))
* per-repo peer web search switch and headless tool hardening ([#77](https://github.com/fantashley/agent-bus/issues/77)) ([2f3e35b](https://github.com/fantashley/agent-bus/commit/2f3e35bf9522e43721f2b74897f9be51da003fa4))
* PR review bridge skill (agent-bus-pr-review); review-only reviews have no follow-up limit ([#150](https://github.com/fantashley/agent-bus/issues/150)) ([393b71b](https://github.com/fantashley/agent-bus/commit/393b71b70a2977e973ebd9b2b2c529b648395449))
* record provenance with every human action and flag suspicious ones ([#131](https://github.com/fantashley/agent-bus/issues/131)) ([742b029](https://github.com/fantashley/agent-bus/commit/742b0296be7c1856db4d9e8d399fada76200d978))
* record token usage per turn and add agent-bus usage ([#61](https://github.com/fantashley/agent-bus/issues/61)) ([38ce49a](https://github.com/fantashley/agent-bus/commit/38ce49a0737fba3fefb92244019890ba5fc25ee6))
* resume threads parked by a usage limit once it resets ([#63](https://github.com/fantashley/agent-bus/issues/63)) ([8fdfe77](https://github.com/fantashley/agent-bus/commit/8fdfe775a0ce5b4d8976ce7e5f577a17ca06a980))
* review convergence with blocking severity, follow-up budget and same-head pushback ([#86](https://github.com/fantashley/agent-bus/issues/86)) ([73b4bdf](https://github.com/fantashley/agent-bus/commit/73b4bdfaccb33675f14e34a00be4b4fd6f971ce3))
* reviewer follow-ups settle findings by stable id, caveats on agree votes, long answers in a body block ([#127](https://github.com/fantashley/agent-bus/issues/127)) ([7b2679d](https://github.com/fantashley/agent-bus/commit/7b2679dca2f784b00e93a87b5fb6aad05649155c))
* reviewer friction: usage ranking, first-pass nudge, range-diff on rebases ([#142](https://github.com/fantashley/agent-bus/issues/142)) ([bbf137c](https://github.com/fantashley/agent-bus/commit/bbf137c5fa28ab693b1e92b625e497e4eda3f55e))
* run Claude Code peers on Amazon Bedrock ([#83](https://github.com/fantashley/agent-bus/issues/83)) ([9009621](https://github.com/fantashley/agent-bus/commit/9009621d8c009e938caef0b7b86be5a0b84e2344))
* run one reviewer per round and require all reviewer approvals ([#57](https://github.com/fantashley/agent-bus/issues/57)) ([7e989f3](https://github.com/fantashley/agent-bus/commit/7e989f3b903bf83b1d8811701611cd34dd719d67))
* search for Antigravity reviewers, numbered pinned reads and review state in every wait ([#121](https://github.com/fantashley/agent-bus/issues/121)) ([4b9689b](https://github.com/fantashley/agent-bus/commit/4b9689b679a3b2554519263656e2dcc44ce6466f))
* send a rebase-aware interdiff when a reviewed change was rebased ([#80](https://github.com/fantashley/agent-bus/issues/80)) ([dfdf283](https://github.com/fantashley/agent-bus/commit/dfdf2833766d63b5440500ef1bd68a6611f8201a))
* separate named participants from CLI harnesses ([#65](https://github.com/fantashley/agent-bus/issues/65)) ([9f912cb](https://github.com/fantashley/agent-bus/commit/9f912cb278fdca0fbfd4819e4c0f7fb906f8d2cf))
* shared read-only evidence on start_discussion and request_review ([#137](https://github.com/fantashley/agent-bus/issues/137)) ([1af775b](https://github.com/fantashley/agent-bus/commit/1af775b4b31afc39473853afc236f81217e37c86))
* ship the agent-bus skill with the build and keep MCP instructions as the core ([#115](https://github.com/fantashley/agent-bus/issues/115)) ([7399400](https://github.com/fantashley/agent-bus/commit/73994009d38ab3a8872d036b25a1f6440207f517))
* usage search keeps specific names, tail --events-only, follow-up verdict examples ([#133](https://github.com/fantashley/agent-bus/issues/133)) ([b5f0911](https://github.com/fantashley/agent-bus/commit/b5f0911b3b8541a1739e6a8696095ce64ff6b8bf))
* wait reports peer progress, skips routine notices and shares concurrent calls ([#88](https://github.com/fantashley/agent-bus/issues/88)) ([0045f6f](https://github.com/fantashley/agent-bus/commit/0045f6f1266475f6b3efd878786f1f7a67c8fec0))
* warn when an agent is close to its usage limit ([#62](https://github.com/fantashley/agent-bus/issues/62)) ([9172d04](https://github.com/fantashley/agent-bus/commit/9172d044660bb0447cd32913a9317e4de5076348))


### Bug Fixes

* adapter abort edges, codex plugins off, keep the session when the fresh retry never launched ([#130](https://github.com/fantashley/agent-bus/issues/130)) ([267e132](https://github.com/fantashley/agent-bus/commit/267e1322a7208b48289146b11314e004771426ae))
* check review commits up front in request_review ([#103](https://github.com/fantashley/agent-bus/issues/103)) ([908ce40](https://github.com/fantashley/agent-bus/commit/908ce400b59cfbc050857726df8dd5582079c27a))
* check the repo allowlist in the worker for every job kind ([#125](https://github.com/fantashley/agent-bus/issues/125)) ([ac9edb5](https://github.com/fantashley/agent-bus/commit/ac9edb5158ef710d9cb485223a7ab5d37c8b0434))
* clearer review and discussion prompts for headless peers ([#102](https://github.com/fantashley/agent-bus/issues/102)) ([0d7bf32](https://github.com/fantashley/agent-bus/commit/0d7bf32e7f55f46bd58338330c42dac1fd7337a9))
* Copilot adapter and Upgrading follow-ups ([#119](https://github.com/fantashley/agent-bus/issues/119)) ([658926e](https://github.com/fantashley/agent-bus/commit/658926eb72848dcfb703f4602859b313dc947109))
* discussion votes without a body, no repeated goal, enforced deadlines ([#105](https://github.com/fantashley/agent-bus/issues/105)) ([f2f9974](https://github.com/fantashley/agent-bus/commit/f2f99746822a00579993d8e91086fcda63874601))
* give shell reviewers read-only jj commands in jj repos ([#104](https://github.com/fantashley/agent-bus/issues/104)) ([faba6f1](https://github.com/fantashley/agent-bus/commit/faba6f1f606b83d1a6988b3a8b5214b566a4fc16))
* headless peers' rules outrank their harness's interactive-mode instructions ([#112](https://github.com/fantashley/agent-bus/issues/112)) ([616e982](https://github.com/fantashley/agent-bus/commit/616e98203e2f5bdda9cd1835952bc8b93a863e9c))
* keep a reviewed checkout's own hooks and config out of peer turns ([#123](https://github.com/fantashley/agent-bus/issues/123)) ([b60db49](https://github.com/fantashley/agent-bus/commit/b60db49a0d6a8f7f5cde4a0e4a8314606f2d1a98))
* keep a single-reviewer review open after request_changes ([#144](https://github.com/fantashley/agent-bus/issues/144)) ([28c704d](https://github.com/fantashley/agent-bus/commit/28c704d06f24374bb842dc37dabe7bdb57c7a1f5))
* keep Claude Code on the current agent-bus build after upgrades ([#85](https://github.com/fantashley/agent-bus/issues/85)) ([59df08d](https://github.com/fantashley/agent-bus/commit/59df08db7f5ab66ea576aa86bf473afeae806966))
* keep session state when the deadline gate skips a turn; hedge the headless final-message note ([#128](https://github.com/fantashley/agent-bus/issues/128)) ([bb6bb07](https://github.com/fantashley/agent-bus/commit/bb6bb07024187e650288b042640696e9f0c5df7b))
* kill the whole process group when a CLI version probe times out ([#56](https://github.com/fantashley/agent-bus/issues/56)) ([16a1e9a](https://github.com/fantashley/agent-bus/commit/16a1e9a896b22b0eb2ef4db51343d4b8a3a6cc76))
* label first looks at later heads as follow-ups and explain pending pushbacks at the limit ([#90](https://github.com/fantashley/agent-bus/issues/90)) ([b97d6a2](https://github.com/fantashley/agent-bus/commit/b97d6a26bcc45f05c12a324ed2c9e96a7b0a8127))
* relaunch queued jobs whose worker never started ([#70](https://github.com/fantashley/agent-bus/issues/70)) ([d59adac](https://github.com/fantashley/agent-bus/commit/d59adacaff94735521b4ef5164768aeb7c4d0ada))
* report missing review commits on the review thread ([#50](https://github.com/fantashley/agent-bus/issues/50)) ([c9d2800](https://github.com/fantashley/agent-bus/commit/c9d28002b75706b79aa95062de31d29d1e09fe25))
* restart the Claude Code MCP server on /reload-plugins after an upgrade ([#94](https://github.com/fantashley/agent-bus/issues/94)) ([18c5617](https://github.com/fantashley/agent-bus/commit/18c56175a955fb7ad77bb71b2d6fbcbafc769082))
* retry CLI version probes after a timeout cooldown ([#49](https://github.com/fantashley/agent-bus/issues/49)) ([54e5192](https://github.com/fantashley/agent-bus/commit/54e5192b5dd20e0057b8e57227e4a0d268a443fb))
* scope single-reviewer follow-up dedupe keys to the thread ([#68](https://github.com/fantashley/agent-bus/issues/68)) ([8587987](https://github.com/fantashley/agent-bus/commit/8587987c35cbfb11068ea9c4355b228445761beb))
* start a follow-up's interdiff at the reviewer's own last verdict ([#79](https://github.com/fantashley/agent-bus/issues/79)) ([1645fbd](https://github.com/fantashley/agent-bus/commit/1645fbdf394f0f9e75c29a0bb42f36be39aa99cd))
* stop spending discussion rounds on peers that can no longer vote ([#51](https://github.com/fantashley/agent-bus/issues/51)) ([4c90088](https://github.com/fantashley/agent-bus/commit/4c90088f90e65fc8358cac89c382534b8ce368ad))
* wait doesn't replay across a client's sessions, reap spawned workers, and other [#126](https://github.com/fantashley/agent-bus/issues/126) follow-ups ([#136](https://github.com/fantashley/agent-bus/issues/136)) ([a3e2c44](https://github.com/fantashley/agent-bus/commit/a3e2c44a096add84ba067565c11efd5fcd0e72eb))

## [0.2.1](https://github.com/fantashley/agent-bus/compare/v0.2.0...v0.2.1) (2026-11-23)


### Bug Fixes

* handle denied tool calls in Antigravity peers ([#45](https://github.com/fantashley/agent-bus/issues/45)) ([e9c6fb0](https://github.com/fantashley/agent-bus/commit/e9c6fb0b32a505512304ebcf518ca0d683169f7b))

## [0.2.0](https://github.com/fantashley/agent-bus/compare/v0.1.0...v0.2.0) (2026-11-22)


### Features

* resilience stage 1, CLI versions, format drift, real fixtures ([#32](https://github.com/fantashley/agent-bus/issues/32)) ([#37](https://github.com/fantashley/agent-bus/issues/37)) ([0c22f94](https://github.com/fantashley/agent-bus/commit/0c22f9496e5f2ae0de70c547363cf7a4de6c6a06))
