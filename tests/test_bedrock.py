"""Peers running Claude Code on Amazon Bedrock (issue #36, agreed on agent-bus thread t-e33f25, P4):
a peer's own non-secret env, no interactive credential refresh, and Bedrock's failures
(expired credentials, a refused model or region, throttling, quotas) told apart."""
import json
import re
import time
from pathlib import Path

import pytest

from agent_bus import autoresume, quota
from agent_bus import config as cfg
from agent_bus.db import HUMAN, SYSTEM, BusError
from agent_bus.peers import StreamState, adapter
from agent_bus.service import Service
from agent_bus.worker import Worker

from test_fixtures import LEAKS, texts

DATA = Path(__file__).parent / "data" / "bedrock"
MANIFEST = json.loads((DATA / "MANIFEST.json").read_text())["fixtures"]
BASE, HEAD = "a" * 40, "b" * 40
BEDROCK = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "bedrock", "AWS_REGION": "us-east-2"}
MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.fixture(autouse=True)
def clean_aws_env(monkeypatch):
    """The worker's own environment must not decide these tests."""
    for k in ("CLAUDE_CODE_USE_BEDROCK", "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(k, raising=False)


def config_path(env):
    return env["tmp"] / "config" / "agent-bus" / "config.json"


def configure(env, claude=None, **keys):
    data = json.loads(config_path(env).read_text())
    if claude is not None:
        data["peers"]["claude"].update(claude)
    data.update(keys)
    config_path(env).write_text(json.dumps(data))


def bedrock(env, **extra):
    configure(env, claude={"env": dict(BEDROCK), "model": MODEL, **extra})


def calls(env, agent=None):
    if not env["log"].exists():
        return []
    out = [json.loads(line) for line in env["log"].read_text().splitlines()]
    return [c for c in out if agent is None or c["agent"] == agent]


def run_job(bus, tid):
    Worker(bus, bus.jobs(tid)[-1]["id"]).run()


def replay(monkeypatch, name, agents="claude", rc=None):
    fx = next(f for f in MANIFEST if f["file"] == name)
    monkeypatch.setenv("FAKE_REPLAY", str(DATA / name))
    monkeypatch.setenv("FAKE_REPLAY_AGENTS", agents)
    monkeypatch.setenv("FAKE_REPLAY_EXIT", str(fx["exit"] if rc is None else rc))


# ------------------------------------------------------------------ env config


@pytest.mark.parametrize("env_value, error", [
    ([], "must be an object"),
    ({"1BAD": "x"}, "invalid variable name"),
    ({"AWS_SECRET_ACCESS_KEY": "x"}, "looks like a credential"),
    ({"AWS_SESSION_TOKEN": "x"}, "looks like a credential"),
    ({"ANTHROPIC_API_KEY": "x"}, "looks like a credential"),
    ({"MY_SERVICE_PASSWORD": "x"}, "looks like a credential"),
    ({"NOTES": "AKIAABCDEFGHIJKLMNOP"}, "looks like it holds a credential"),
    ({"NOTES": "sk-ant-abcdefghijklmnopqrstuvwx"}, "looks like it holds a credential"),
    ({"AWS_PROFILE": 3}, "must be a string"),
    ({"AWS_REGION": "us east"}, "not an AWS region"),
    ({"AGENT_BUS_PEER": "1"}, "set by agent-bus itself"),
    ({"CODEX_HOME": "/x"}, "use codex_home"),
])
def test_env_is_validated_without_echoing_values(env, env_value, error):
    configure(env, claude={"env": env_value})
    with pytest.raises(ValueError, match=error) as e:
        cfg.Config.load()
    for secret in ("AKIAABCDEFGHIJKLMNOP", "sk-ant-abcdefghijklmnopqrstuvwx"):
        assert secret not in str(e.value)


def test_env_allows_ordinary_settings_and_merges_per_variable(env):
    configure(env, claude={"env": {**BEDROCK, "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL,
                                   "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "4096",
                                   "AWS_SHARED_CREDENTIALS_FILE": "/home/user/.aws/credentials"}},
              participants={"west": {"harness": "claude", "env": {"AWS_REGION": "us-west-2"}}})
    conf = cfg.Config.load()
    assert conf.peer_env("west")["AWS_REGION"] == "us-west-2"
    assert conf.peer_env("west")["AWS_PROFILE"] == "bedrock"  # inherited from peers.claude
    assert conf.peer_env("claude")["AWS_REGION"] == "us-east-2"
    assert conf.bedrock("west") and conf.aws_region("west") == "us-west-2"
    assert conf.peer_env("codex") == {} and not conf.bedrock("codex")


def test_another_aws_profile_or_provider_needs_its_own_account(env):
    for participants, error in [
        ({"work": {"harness": "claude", "env": {"AWS_PROFILE": "other"}}}, "set participants.work.account"),
        ({"plain": {"harness": "claude", "env": {"CLAUDE_CODE_USE_BEDROCK": "0"}}}, "set participants.plain.account"),
        ({"a": {"harness": "claude", "env": {"AWS_PROFILE": "x"}, "account": "w"},
          "b": {"harness": "claude", "env": {"AWS_PROFILE": "y"}, "account": "w"}}, "same account w"),
    ]:
        bedrock(env)
        configure(env, participants=participants)
        with pytest.raises(ValueError, match=error):
            cfg.Config.load()
    configure(env, participants={"west": {"harness": "claude", "env": {"AWS_REGION": "us-west-2"}},
                                 "work": {"harness": "claude", "env": {"AWS_PROFILE": "other"}, "account": "work"}})
    conf = cfg.Config.load()  # a region alone is the same login
    assert quota.subject(conf, "west") == "claude" and quota.subject(conf, "work") == "claude@work"


def test_bedrock_inference_profile_arns_fit_the_model_limit(env):
    arn = ("arn:aws:bedrock:ap-southeast-2:123456789012:inference-profile/"
           "global.anthropic.claude-sonnet-4-5-20250929-v1:0")
    app = "arn:aws:bedrock:us-east-2:123456789012:application-inference-profile/abcdef123456"
    assert len(arn) > 100
    for model in (arn, app, "global.anthropic.claude-opus-5-5"):
        assert cfg.check_peer_settings("claude", {"model": model}) == {"model": model}
    with pytest.raises(ValueError):
        cfg.check_peer_settings("claude", {"model": "a" * 257})


# --------------------------------------------------------------- the launch


def test_turn_gets_the_env_and_agent_bus_owned_settings(env, monkeypatch):
    bedrock(env, args=["--settings", '{"awsAuthRefresh": "aws sso login"}', "--verbose"])
    monkeypatch.setenv("FAKE_LOG_ENV", "AWS_PROFILE,AWS_REGION,CLAUDE_CODE_USE_BEDROCK")
    conf = cfg.Config.load()
    ad = adapter("claude", conf)
    assert ad.recipe == "6"
    res = ad.run("hello", str(env["repo"]), 30, lambda k, t: None)
    assert res.ok, res.error
    call = calls(env, "claude")[-1]
    assert call["env"] == BEDROCK
    argv = call["argv"]
    assert argv.count("--settings") == 1  # the configured one is dropped
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings == {"awsAuthRefresh": "", "env": BEDROCK}
    # Without any env the refresh is still switched off: user settings may pick Bedrock.
    configure(env, claude={"env": {}, "args": []})
    argv = adapter("claude", cfg.Config.load()).invocation("p", "sid", str(env["repo"]), []).argv
    assert json.loads(argv[argv.index("--settings") + 1]) == {"awsAuthRefresh": ""}


def test_env_reaches_codex_and_changes_fingerprints_only_when_set(env, monkeypatch):
    configure(env, participants={"west": {"harness": "claude", "env": {"AWS_REGION": "us-west-2"}}})
    conf = cfg.Config.load()
    assert adapter("claude", conf).launch_fingerprint() != adapter("west", conf).launch_fingerprint()
    data = json.loads(config_path(env).read_text())
    data["peers"]["codex"].update(env={"HTTPS_PROXY": "http://proxy:3128"}, codex_home="/x")
    config_path(env).write_text(json.dumps(data))
    ad = adapter("codex", cfg.Config.load())
    assert ad.child_env() == {"HTTPS_PROXY": "http://proxy:3128", "CODEX_HOME": "/x"}


def test_antigravity_participant_with_its_own_env_needs_allow_mcp(env):
    from agent_bus.peers import PeerSetupError
    configure(env, participants={"agy-proxy": {"harness": "antigravity", "env": {"HTTPS_PROXY": "http://p:1"}}})
    with pytest.raises(PeerSetupError, match="own command, args or env"):
        adapter("agy-proxy", cfg.Config.load()).check_isolation()


# ------------------------------------------------------- captured failures


@pytest.mark.parametrize("fx", MANIFEST, ids=[f["file"] for f in MANIFEST])
def test_captured_bedrock_failures_are_classified(env, monkeypatch, fx):
    bedrock(env)
    replay(monkeypatch, fx["file"])
    events = []
    res = adapter("claude", cfg.Config.load(), {"model": MODEL}).run(
        "hello", str(env["repo"]), 30, lambda k, t: events.append((k, t)))
    assert not res.ok and not res.format_changed
    if fx["expect"] == "auth":
        assert res.quota == {"resets_at": None, "source": "auth"} and not res.config_error
    elif fx["expect"] == "config":
        assert res.config_error and res.quota is None
    else:  # still retrying when it was killed: nothing to classify, but the retries show
        assert res.quota is None and not res.config_error
    retries = [t for k, t in events if k == "api_retry"]
    if fx["expect"] == "retrying":
        assert retries[0] == "api retry 1/10: unknown"
    if fx["expect"] == "auth":
        assert retries == ["api retry 1/2: cloud_credential_error", "api retry 2/2: cloud_credential_error"]
    assert not [t for k, t in events if k == "format"], events


@pytest.mark.parametrize("fx", MANIFEST, ids=[f["file"] for f in MANIFEST])
def test_bedrock_fixtures_leak_nothing(fx):
    for text in texts(DATA / fx["file"]):
        for pattern, what in LEAKS:
            assert not re.search(pattern, text), (fx["file"], what, text[:120])
        # No real AWS account id: only the documented placeholders.
        for acct in re.findall(r"arn:aws:[a-z-]*:[a-z0-9-]*:(\d{12})", text):
            assert acct in ("000000000000", "123456789012"), fx["file"]


def test_manifest_lists_every_bedrock_fixture():
    assert {f["file"] for f in MANIFEST} == {p.name for p in DATA.glob("*.jsonl")}


# --------------------------------------------- documented (not captured) shapes


def test_documented_throttle_and_quota_errors_are_scoped_to_model_and_region(env):
    """Synthetic: AWS documents these, they weren't captured (they need load)."""
    bedrock(env)
    ad = adapter("claude", cfg.Config.load())
    throttled = "API Error: 429 ThrottlingException: Too many requests, please wait before trying again."
    assert ad.classify(StreamState(), throttled)["source"] == "throttle"
    assert ad.classify(StreamState(api_status=429), "API Error: 429 rate exceeded")["source"] == "throttle"
    for text in ("API Error: 429 Too many tokens per day, please wait before trying again.",
                 "ServiceQuotaExceededException: quota exceeded for model"):
        assert ad.classify(StreamState(), text)["source"] == "heuristic"
    denied = ("API Error: 403 AccessDeniedException: You don't have access to the model with the specified "
              "model ID.")
    assert ad.classify(StreamState(), denied) is None and ad.misconfigured(StreamState(), denied)
    expired = "ExpiredTokenException: The security token included in the request is expired"
    assert ad.classify(StreamState(), expired)["source"] == "auth"
    # Not on Bedrock: a bare 429 stays an ordinary error, as before (#26).
    configure(env, claude={"env": {}})
    assert adapter("claude", cfg.Config.load()).classify(StreamState(), throttled) is None


def test_throttle_blocks_only_that_model_and_region_briefly(env, no_spawn, monkeypatch):
    bus = env["bus"]
    bedrock(env)
    configure(env, participants={"west": {"harness": "claude", "env": {"AWS_REGION": "us-west-2"}}})
    conf = cfg.Config.load()
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    me = Service(bus, "codex", bus.register_session("codex", env={}))
    # Synthetic, in the shape of the captured failures: Claude Code's retries spent on a 429.
    stream = env["tmp"] / "throttled.jsonl"
    stream.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                                  "session_id": "00000000-0000-4000-8000-000000000009",
                                  "result": "API Error: 429 ThrottlingException: Too many requests"}) + "\n")
    monkeypatch.setenv("FAKE_REPLAY", str(stream))
    monkeypatch.setenv("FAKE_REPLAY_AGENTS", "claude")
    tid = me.request_review("claude", str(env["repo"]), BASE, HEAD)["thread"]["id"]
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human" and len(calls(env, "claude")) == 1
    block = bus.quota_block("claude", f"{MODEL}@us-east-2")
    assert block["scope"] == f"{MODEL}@us-east-2" and block["until"] <= time.time() + quota.THROTTLE_COOLDOWN_S + 1
    assert quota.block_for(bus, conf, "claude", {"model": MODEL})
    assert quota.block_for(bus, conf, "west", {"model": MODEL}) is None  # same account, other region
    assert quota.block_for(bus, conf, "claude", {"model": "global.anthropic.claude-opus-5-5"}) is None
    assert "throttled by its model provider" in quota.describe("claude", block)


# --------------------------------------------------------- worker behaviour


def test_expired_credentials_block_the_account_until_the_human_retries(env, no_spawn, monkeypatch):
    bus = env["bus"]
    bedrock(env)
    replay(monkeypatch, "claude-sso-token-missing.jsonl")
    monkeypatch.setattr("agent_bus.worker.Worker.review_diff", lambda self, refs, interdiff: ("diff", "Diff"))
    me = Service(bus, "codex", bus.register_session("codex", env={}))
    tid = me.request_review("claude", str(env["repo"]), BASE, HEAD)["thread"]["id"]
    run_job(bus, tid)
    assert bus.thread(tid)["status"] == "needs_human"
    assert len(calls(env, "claude")) == 1  # no fresh session, no repair turn
    block = bus.quota_block("claude", f"{MODEL}@us-east-2")
    assert block["source"] == "auth" and block["scope"] == "" and block["until"] > time.time() + 86400
    assert not env["waiters"] and autoresume.pending(bus) == []  # never resumes by itself
    notices = [m["body"] for m in bus.history(tid)["messages"] if m["sender"] == SYSTEM]
    auth = [n for n in notices if n.startswith("AUTH_REQUIRED")]
    assert len(auth) == 1
    assert "`aws sso login --profile bedrock`" in auth[0] and f"agent-bus resume {tid}" in auth[0]
    assert not any("SSO session token" in n for n in notices)  # the CLI's own text stays in events

    with pytest.raises(BusError) as e:  # fails fast for every model of that account
        me.request_review("claude", str(env["repo"]), BASE, "c" * 40, model="global.anthropic.claude-opus-5-5")
    assert e.value.code == "AUTH_REQUIRED" and "aws sso login --profile bedrock" in e.value.message

    monkeypatch.delenv("FAKE_REPLAY")
    Service(bus, HUMAN).resume(tid)
    assert bus.quota_block("claude") is None


def test_refused_model_gets_a_fixed_notice_and_no_retry_or_repair(env, no_spawn, monkeypatch):
    bus = env["bus"]
    bedrock(env)
    replay(monkeypatch, "claude-invalid-model-id.jsonl")
    me = Service(bus, "codex", bus.register_session("codex", env={}))
    tid = me.start_discussion("q?", peers=["claude"])["thread"]["id"]
    run_job(bus, tid)
    assert len(calls(env, "claude")) == 1
    # Its only peer can't vote again: the discussion parks instead of spending rounds.
    assert bus.thread(tid)["status"] == "needs_human"
    notices = [m["body"] for m in bus.history(tid)["messages"] if m["sender"] == SYSTEM]
    bad = [n for n in notices if n.startswith("PEER_MISCONFIGURED")]
    assert len(bad) == 1 and MODEL in bad[0] and "us-east-2" in bad[0]
    assert not any("model identifier is invalid" in n for n in notices)
    assert bus.quota_blocks() == []  # no block: only this discussion remembers it
    assert not env["waiters"]  # and it doesn't resume by itself
    from agent_bus import unvotable
    stuck = unvotable.peers(bus, cfg.Config.load(), bus.thread(tid))
    assert stuck["claude"]["why"] == "config" and unvotable.code(stuck) == "PEER_MISCONFIGURED"
    # The human's resume clears it, and the peer is asked again.
    monkeypatch.delenv("FAKE_REPLAY")
    Service(bus, HUMAN).resume(tid)
    assert bus.peer_drift(tid) == {}


def test_result_status_429_alone_is_a_throttle(env, monkeypatch):
    bedrock(env)
    stream = env["tmp"] / "429.jsonl"
    stream.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                                  "session_id": "00000000-0000-4000-8000-000000000009",
                                  "result": "API Error: 429 rate exceeded"}) + "\n")
    monkeypatch.setenv("FAKE_REPLAY", str(stream))
    monkeypatch.setenv("FAKE_REPLAY_AGENTS", "claude")
    res = adapter("claude", cfg.Config.load()).run("hello", str(env["repo"]), 30, lambda k, t: None)
    assert res.quota["source"] == "throttle"


def test_configured_anthropic_model_scopes_blocks(env):
    configure(env, claude={"env": {**BEDROCK, "ANTHROPIC_MODEL": "global.anthropic.claude-opus-5-5"}},
              participants={"son": {"harness": "claude",
                                    "env": {"ANTHROPIC_MODEL": "global.anthropic.claude-sonnet-5-5"}}})
    conf = cfg.Config.load()
    assert quota.effective_model(conf, "claude", {}) == "global.anthropic.claude-opus-5-5"
    assert quota.model_scope(conf, "son", quota.effective_model(conf, "son", {})) == \
        "global.anthropic.claude-sonnet-5-5@us-east-2"
    assert quota.effective_model(conf, "claude", {"model": MODEL}) == MODEL  # an explicit model wins


def test_other_aws_config_files_are_another_login(env):
    for var in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "CLAUDE_CONFIG_DIR", "HOME"):
        bedrock(env)
        configure(env, participants={"other": {"harness": "claude", "env": {var: "/etc/other"}}})
        with pytest.raises(ValueError, match="set participants.other.account"):
            cfg.Config.load()


def test_auth_failure_after_an_allowed_rate_limit_reading(env):
    st = StreamState(rate_limit={"status": "allowed"}, api_error="authentication_failed")
    assert adapter("claude", cfg.Config.load()).classify(st, "API Error: 401")["source"] == "auth"


def test_path_is_not_a_peer_env_variable(env):
    configure(env, claude={"env": {"PATH": "/opt/bin"}})
    with pytest.raises(ValueError, match="set command"):
        cfg.Config.load()


def test_antigravity_mcp_check_follows_the_peer_env(env, tmp_path):
    from agent_bus.peers import PeerSetupError
    data = json.loads(config_path(env).read_text())
    for bad in ({"AGY_CONFIG_DIR": "relative/agy"}, {"AGY_CONFIG_DIR": "~/agy"}, {"HOME": ""}):
        data["peers"]["antigravity"]["env"] = bad
        config_path(env).write_text(json.dumps(data))
        with pytest.raises(PeerSetupError, match="must be absolute"):
            adapter("antigravity", cfg.Config.load()).check_isolation()
    other = tmp_path / "other-agy"
    other.mkdir()
    (other / "mcp_config.json").write_text(json.dumps({"mcpServers": {"github": {}}}))
    data["peers"]["antigravity"]["env"] = {"AGY_CONFIG_DIR": str(other)}
    config_path(env).write_text(json.dumps(data))
    with pytest.raises(PeerSetupError, match="github"):  # the child's directory, not the worker's
        adapter("antigravity", cfg.Config.load()).check_isolation()


def test_aliases_scope_by_the_configured_model_and_hints_use_the_config_file(env):
    configure(env, claude={"env": {**BEDROCK, "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL, "HOME": "/srv/peer",
                                   "AWS_CONFIG_FILE": "/etc/aws peer/config"}})
    conf = cfg.Config.load()
    assert quota.model_scope(conf, "claude", "haiku") == f"{MODEL}@us-east-2"
    assert quota.auth_hint(conf, "claude") == \
        "run `HOME=/srv/peer AWS_CONFIG_FILE='/etc/aws peer/config' aws sso login --profile bedrock` on this machine"

