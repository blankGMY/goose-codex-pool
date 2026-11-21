#!/usr/bin/env python3
"""One-off test: can Codex CLI on the PC run with an auth.json that has
NO refresh_token (access-token-only lend from the pool's active account)?

Builds the delegate auth.json from the pool's stored auth.json for the
active account, strips refresh_token, copies it to the PC's delegate
CODEX_HOME, and runs a trivial `codex exec` there. Prints only
non-secret facts (account prefix, expiry, exit code, model output).
"""
import json
import os
import subprocess
import sys

POOL = os.path.expanduser("~/goose/pool")
DELEGATE_HOME_WIN = r"C:\Users\10547\codex-delegate\codex-home"
TEST_DIR_WIN = r"C:\Users\10547\codex-worker\delegate-test"
CODEX_CMD = r"C:\Users\10547\AppData\Roaming\npm\codex.cmd"


def run(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = (p.stdout or b"").decode("utf-8", "replace") + \
          (p.stderr or b"").decode("utf-8", "replace")
    return p.returncode, out


def main():
    pool = json.load(open(os.path.join(POOL, "pool.json")))
    active = pool["active"]
    acct = next(a for a in pool["accounts"] if a["id"] == active)
    print("active:", active, "grant_shared:", acct.get("grant_shared"))
    auth = json.load(open(os.path.join(POOL, "accounts", active, "auth.json")))
    toks = json.load(open(os.path.join(POOL, "accounts", active, "tokens.json")))
    print("account_id prefix:", str(auth["tokens"].get("account_id"))[:8])
    print("access token expires_at:", toks.get("expires_at"))

    auth["tokens"]["refresh_token"] = None
    local = "/tmp/delegate-auth-test.json"
    with open(local, "w") as f:
        json.dump(auth, f)
    os.chmod(local, 0o600)

    rc, out = run(["ssh", "-o", "BatchMode=yes", "winpc",
                   "mkdir " + DELEGATE_HOME_WIN], timeout=30)
    print("mkdir rc:", rc)
    rc, out = run(["scp", "-o", "BatchMode=yes", local,
                   "winpc:" + DELEGATE_HOME_WIN.replace("\\", "/") + "/auth.json"],
                  timeout=60)
    print("scp rc:", rc, out.strip()[:200])
    if rc != 0:
        print("SCP FAILED")
        return 1

    cmd = ('set "CODEX_HOME=' + DELEGATE_HOME_WIN + '" && cd /d ' + TEST_DIR_WIN +
           ' && ' + CODEX_CMD +
           ' exec --sandbox read-only --skip-git-repo-check "Reply with exactly: PONG"')
    rc, out = run(["ssh", "-o", "BatchMode=yes", "winpc", cmd], timeout=300)
    print("codex rc:", rc)
    print("codex output tail:", out.strip()[-600:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
