#!/usr/bin/env python3
"""Variant test: find an auth.json shape that lets Codex CLI run on a
lent access token (no usable refresh token on the PC).

Variants (all with last_refresh bumped to now, which run #1 lacked):
  V1: refresh_token key omitted
  V2: refresh_token = "" (empty string)
  V3: refresh_token = null (same as run #1 but with fresh last_refresh)
Stops at the first variant whose `codex exec` returns rc 0.
Prints only non-secret facts.
"""
import copy
import json
import os
import subprocess
import sys
import time

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
    base = json.load(open(os.path.join(POOL, "accounts", active, "auth.json")))
    print("active:", active, flush=True)

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    variants = []
    v1 = copy.deepcopy(base)
    v1["tokens"].pop("refresh_token", None)
    v1["last_refresh"] = now_iso
    variants.append(("V1 omit-refresh + fresh last_refresh", v1))
    v2 = copy.deepcopy(base)
    v2["tokens"]["refresh_token"] = ""
    v2["last_refresh"] = now_iso
    variants.append(("V2 empty-refresh + fresh last_refresh", v2))
    v3 = copy.deepcopy(base)
    v3["tokens"]["refresh_token"] = None
    v3["last_refresh"] = now_iso
    variants.append(("V3 null-refresh + fresh last_refresh", v3))

    for name, auth in variants:
        local = "/tmp/delegate-auth-variant.json"
        with open(local, "w") as f:
            json.dump(auth, f)
        os.chmod(local, 0o600)
        rc, out = run(["scp", "-o", "BatchMode=yes", local,
                       "winpc:" + DELEGATE_HOME_WIN.replace("\\", "/") + "/auth.json"],
                      timeout=60)
        print(name, "scp rc:", rc, flush=True)
        if rc != 0:
            continue
        cmd = ('set "CODEX_HOME=' + DELEGATE_HOME_WIN + '" && cd /d ' + TEST_DIR_WIN +
               ' && ' + CODEX_CMD +
               ' exec --sandbox read-only --skip-git-repo-check "Reply with exactly: PONG"')
        rc, out = run(["ssh", "-o", "BatchMode=yes", "winpc", cmd], timeout=280)
        tail = out.strip()[-300:].replace("\n", " | ")
        print(name, "codex rc:", rc, "tail:", tail, flush=True)
        if rc == 0:
            print("WINNER:", name, flush=True)
            return 0
    print("NO VARIANT WORKED", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
