"""Scrub a raw CLI stream capture into a committable fixture (issue #32).

    python tests/data/streams/scrub.py RAW.jsonl OUT.jsonl --repo /path/used/as/cwd

Keeps every event, key and the event order; replaces ids (in values and keys) with
stable placeholders, local paths with /repo and /home/user, thinking signatures with
"redacted", account-specific values (quota utilization, reset times, credential
expiry, socket paths) with synthetic ones, and trims long lists to three entries.

It works one string at a time, so text a CLI streams in fragments (Antigravity's
text_delta) can still spell out a path once joined.  Capture with prompts whose
replies don't echo paths, and review the output by hand before committing;
tests/test_fixtures.py also checks the joined text.  Raw captures stay private.
"""
import argparse
import json
import os
import re

UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
PREFIXED = re.compile(r"\b(msg|toolu|req|call|item)_[A-Za-z0-9]{6,}\b")

# Account-specific values, replaced with synthetic ones of the same type.
SYNTHETIC = {
    "resetsAt": 4102444800,
    "utilization": 0.5,
    "credential_expires_at": "2099-01-01T00:00:00.000Z",
    "messaging_socket_path": "/run/user/1000/agent.sock",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    ap.add_argument("out")
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()
    ids: dict[str, str] = {}

    def stable(m: re.Match) -> str:
        key = m.group(0)
        if key not in ids:
            n = len(ids) + 1
            ids[key] = (f"00000000-0000-4000-8000-{n:012d}" if UUID.fullmatch(key)
                        else f"{m.group(1)}_fixture{n:04d}")
        return ids[key]

    home = os.path.expanduser("~")

    def scrub(v, key=""):
        if key in SYNTHETIC:
            return SYNTHETIC[key]
        if isinstance(v, dict):
            # Keys too: some CLIs key maps by tool-call ids.
            return {scrub(k): ("redacted" if k == "signature" else scrub(x, k)) for k, x in v.items()}
        if isinstance(v, list):
            return [scrub(x, key) for x in v[:3]]
        if isinstance(v, str):
            v = v.replace(os.path.realpath(args.repo), "/repo").replace(args.repo, "/repo").replace(home, "/home/user")
            v = re.sub(r"/tmp/claude-\d+/[^\s\"']*", "/tmp/scratch", v)
            # Paths encoded as directory names (Claude's project dirs): -tmp-…, -home-<user>-…
            v = re.sub(r"-tmp-claude-\d+-[^\s\"'/]*", "-tmp-scratch", v)
            user = os.path.basename(home)
            v = re.sub(rf"-home-{re.escape(user)}\b", "-home-user", v).replace(user, "user")
            return PREFIXED.sub(stable, UUID.sub(stable, v))
        return v

    with open(args.raw) as src, open(args.out, "w") as dst:
        for line in src:
            line = line.strip()
            if line:
                dst.write(json.dumps(scrub(json.loads(line)), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
