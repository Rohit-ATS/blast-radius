"""Fault-injection check: does the API survive HydraDB disappearing?

Uptime is not proved by a system that has never been disturbed. This stops the
database underneath the running server and asserts three things:

  1. while it is down, requests fail *fast and cleanly* (503 with a hint),
     not with a 500, a stack trace, or a hang that ties up the worker pool
  2. endpoints backed only by the sidecar keep working throughout
  3. when the database comes back, the server recovers on its own — no restart,
     no manual intervention, stale pooled connections notwithstanding

Run:  py chaos.py            (takes ~2 minutes; it really does stop the container)
"""

import argparse
import subprocess
import sys
import time

import requests

BASE = "http://127.0.0.1:8000"
GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    mark = f"{GREEN}PASS{OFF}" if ok else f"{RED}FAIL{OFF}"
    print(f"  [{mark}] {name}" + (f"  {DIM}{detail}{OFF}" if detail else ""))


def hit(path, timeout=60, **params):
    t0 = time.perf_counter()
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=timeout)
        return r.status_code, (time.perf_counter() - t0) * 1000, r
    except requests.RequestException as e:
        return e.__class__.__name__, (time.perf_counter() - t0) * 1000, None


def docker(*args):
    return subprocess.run(["docker", "compose", *args], capture_output=True,
                          text=True, timeout=180)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--recover-timeout", type=int, default=300)
    args = p.parse_args()

    print("\n1. baseline — everything up")
    code, ms, r = hit("/api/blast", name="debug", depth=3)
    check("blast works before the fault", code == 200, f"HTTP {code} in {ms:.0f}ms")
    code, ms, _ = hit("/api/stats")
    check("stats works before the fault", code == 200, f"HTTP {code} in {ms:.0f}ms")

    print("\n2. stopping hydradb")
    out = docker("stop", "hydradb")
    check("container stopped", out.returncode == 0, out.stderr.strip()[-80:])
    time.sleep(2)

    print("\n3. behaviour while the database is gone")
    code, ms, r = hit("/api/blast", timeout=120, name="debug", depth=3)
    check("graph endpoint fails fast, not hanging", isinstance(code, int) and ms < 60_000,
          f"{code} in {ms:.0f}ms")
    check("graph endpoint returns 503, not 500", code == 503, f"HTTP {code}")
    if r is not None and code == 503:
        body = r.json()
        check("503 body explains and hints",
              "hydradb" in str(body).lower() and "hint" in body,
              str(body.get("hint", ""))[:60])

    code, ms, r = hit("/api/stats", timeout=60)
    check("sidecar-backed stats still serves during the outage", code == 200,
          f"HTTP {code} in {ms:.0f}ms")
    code, _, _ = hit("/api/search", q="deb")
    check("search still serves during the outage", code == 200, f"HTTP {code}")
    code, _, _ = hit("/api/maintainers", name="debug")
    check("maintainer pivot still serves during the outage", code == 200,
          f"HTTP {code}")
    code, _, _ = hit("/")
    check("console still serves during the outage", code == 200, f"HTTP {code}")

    print("\n4. restarting hydradb")
    out = docker("start", "hydradb")
    check("container started", out.returncode == 0, out.stderr.strip()[-80:])

    print("\n5. recovery without touching the server")
    # Measured on this machine: HydraDB serves /readyz within a second of
    # starting but cannot complete a depth-5 traversal for ~93s afterwards,
    # failing its own 30-second query timeout while the store pages in. So
    # recovery is checked against that reality rather than an optimistic
    # few seconds — and the server is expected to *say* it is warming.
    t_restart = time.time()
    deadline = t_restart + args.recover_timeout
    recovered_at = None
    saw_warming = False
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        code, ms, r = hit("/api/blast", timeout=90, name="debug", depth=5)
        if code == 200:
            recovered_at = time.time()
            break
        if code == 503 and r is not None and r.json().get("error") == "graph_warming":
            saw_warming = True
        print(f"    {DIM}t+{time.time() - t_restart:4.0f}s -> {code}{OFF}")
        time.sleep(5)
    check("graph endpoint recovers by itself", recovered_at is not None,
          f"after {attempts} attempts, t+{recovered_at - t_restart:.0f}s"
          if recovered_at else f"still failing after {args.recover_timeout}s")
    check("outage reported as warming, not as a hard failure", saw_warming,
          "" if saw_warming else "never saw error=graph_warming")

    if recovered_at:
        ok = 0
        for _ in range(5):
            code, _, _ = hit("/api/blast", timeout=90, name="debug", depth=5)
            ok += code == 200
        check("stable after recovery", ok == 5, f"{ok}/5 follow-up requests")

    print(f"\n{'=' * 70}")
    bad = [r for r in results if not r[1]]
    colour = GREEN if not bad else RED
    print(f"{colour}{len(results) - len(bad)}/{len(results)} checks passed{OFF}")
    for name, _, detail in bad:
        print(f"  - {name}: {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
