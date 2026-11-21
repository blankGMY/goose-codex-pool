#!/usr/bin/env python3
"""Keep the PC delegation channel's Codex account aligned with the
pool's active account ("same account until it runs out, then switch").

Runs on the Pi (cron, a couple of minutes after the pool patrol so the
harvest has landed). Two modes, by the active account's grant type:

- Isolated grant (grant_shared=false): build a delegate auth.json from
  the account's pool-side auth.json and scp it to the PC's delegation
  CODEX_HOME (C:\\Users\\10547\\codex-delegate\\codex-home\\auth.json).
  The refresh_token is replaced with an EMPTY STRING (verified
  2026-11-22: null or a missing key makes the CLI's auth struct fail
  to load and it falls back to unauthenticated API mode; "" loads fine
  and the CLI runs on the access token). last_refresh is bumped to the
  lend time. Iron rule: one refresh-token family has exactly one
  refresher. The pool/Goose side owns this family, so the PC can spend
  quota but can never refresh - it can never trip OpenAI's token-reuse
  revocation.
- Shared grant (grant_shared=true): the family belongs to the PC's own
  Codex login, so the freshest copy already lives on the PC. Trigger
  the PC-side copy-auth.ps1, which mirrors ~/.codex/auth.json into the
  delegation CODEX_HOME when it changed.

The user's interactive Codex login (~/.codex) is never written by this
script. State lives in delegate-sync-state.json next to pool.json;
every run appends one line to delegate-sync.log. Secrets never print.
"""
import hashlib
import json
import os
import subprocess
import sys
import time

POOL_DIR = os.path.expanduser("~/goose/pool")
STATE = os.path.join(POOL_DIR, "delegate-sync-state.json")
LOG = os.path.join(POOL_DIR, "delegate-sync.log")
WIN_HOME = "C:/Users/10547/codex-delegate/codex-home"
OWN_COPY_MIN_INTERVAL = 1800  # seconds between grant_shared copy triggers


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def run(cmd, timeout=60):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = (p.stdout or b"").decode("utf-8", "replace") + \
          (p.stderr or b"").decode("utf-8", "replace")
    return p.returncode, out.strip()


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def main():
    pool = load_json(os.path.join(POOL_DIR, "pool.json"))
    if not pool or not pool.get("active"):
        log("no pool state; skip")
        return 1
    active = pool["active"]
    acct = next((a for a in pool["accounts"] if a["id"] == active), None)
    if acct is None:
        log("active account %s not in pool; skip" % active)
        return 1
    state = load_json(STATE, {}) or {}
    now = int(time.time())

    if acct.get("grant_shared"):
        last = state.get("own_copy_at", 0) if state.get("account") == active else 0
        if now - last < OWN_COPY_MIN_INTERVAL:
            log("%s shared-grant; own-copy still fresh; skip" % active)
            return 0
        rc, out = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                       "winpc",
                       r"powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\10547\codex-delegate\copy-auth.ps1"],
                      timeout=60)
        if rc != 0:
            log("%s shared-grant; own-copy FAILED rc=%s %s" % (active, rc, out[:120]))
            return 1
        state.update({"account": active, "mode": "own", "own_copy_at": now})
        with open(STATE, "w") as f:
            json.dump(state, f)
        os.chmod(STATE, 0o600)
        log("%s shared-grant; own-copy triggered (%s)" % (active, out[:40]))
        return 0

    auth = load_json(os.path.join(POOL_DIR, "accounts", active, "auth.json"))
    if not auth or not auth.get("tokens", {}).get("access_token"):
        log("%s no stored access token; skip" % active)
        return 1
    access = auth["tokens"]["access_token"]
    account_id = str(auth["tokens"].get("account_id"))
    digest = hashlib.sha256((account_id + "\n" + access).encode()).hexdigest()
    if state.get("account") == active and state.get("sha256") == digest:
        log("%s lend unchanged; skip" % active)
        return 0
    auth["tokens"]["refresh_token"] = ""
    auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    blob = json.dumps(auth, sort_keys=True).encode()
    tmp = "/tmp/delegate-auth.json"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.chmod(tmp, 0o600)
    rc, out = run(["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                   tmp, "winpc:" + WIN_HOME + "/auth.json"], timeout=60)
    if rc != 0:
        log("%s lend FAILED rc=%s %s" % (active, rc, out[:120]))
        return 1
    state.update({"account": active, "mode": "lend", "sha256": digest,
                  "lent_at": now})
    with open(STATE, "w") as f:
        json.dump(state, f)
    os.chmod(STATE, 0o600)
    log("%s lend updated (access token only)" % active)
    return 0


if __name__ == "__main__":
    sys.exit(main())
