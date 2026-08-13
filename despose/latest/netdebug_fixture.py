"""Quiet network/console instrumentation for D365 Playwright tests.

Paste into field/tests/conftest.py (or import it there). Autouse.

By default it prints NOTHING for healthy traffic. It prints:
  * failed requests (Chromium net errors)      — first occurrence only, then counted
  * HTTP >= 400 responses                       — first occurrence only, then counted
  * console errors and uncaught page errors     — deduped, D365's own noise muted
  * one summary table at the end, per host

Environment switches:
  NETDEBUG_VERBOSE=1   log every request/response, like the original fixture
  NETDEBUG_TRACE=1     also record a Playwright trace to test-results/
  NETDEBUG_HOSTS=a,b   extra hostname substrings to always report on

Run:
    NETDEBUG_TRACE=1 .venv/bin/python -m pytest field/tests/web/test_x.py \
        -v -s --no-header --dist=no -p no:allure_pytest --import-mode=importlib -o addopts=
"""

import os
import re
import time
from collections import defaultdict
from urllib.parse import urlparse

import pytest

VERBOSE = os.getenv("NETDEBUG_VERBOSE") == "1"
TRACE = os.getenv("NETDEBUG_TRACE") == "1"

# Hosts always named in the summary even when healthy, so "not requested at all"
# is distinguishable from "requested and fine". Extend with NETDEBUG_HOSTS.
KEY_HOSTS = [h for h in ("apps.powerapps.com",) + tuple(
    x.strip() for x in os.getenv("NETDEBUG_HOSTS", "").split(",") if x.strip()) if h]

# The single request whose failure breaks the form. Reported explicitly.
CRITICAL = "clientsdkproxy"

# D365 talks to itself a lot and complains while doing it. None of these indicate
# a problem with the test; muting them is what makes the real errors visible.
MUTE = re.compile(
    r"re-registered|registerIcons|unmountComponentAtNode|not a top-level container"
    r"|ClientBrowserDataSource|No previously cached ECS|fallback to sending event",
    re.I,
)


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or "?"
    except ValueError:
        return "?"


def _short(text: str, n: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "…"


@pytest.fixture(autouse=True)
def netdebug(page, request):
    t0 = time.time()
    stats = defaultdict(lambda: {"req": 0, "ok": 0, "bad": 0, "err": None})
    seen = defaultdict(int)          # dedupe key -> times seen
    critical = {"requested": False, "outcome": None}

    def stamp() -> str:
        return f"[{time.time() - t0:6.1f}s]"

    def once(key: str) -> bool:
        """True the first time a given problem is seen; counts silently after."""
        seen[key] += 1
        return seen[key] == 1

    def on_request(req):
        h = _host(req.url)
        stats[h]["req"] += 1
        if CRITICAL in req.url:
            critical["requested"] = True
        if VERBOSE:
            print(f"{stamp()} REQ    {req.resource_type:10} {req.url[:120]}")

    def on_response(resp):
        h = _host(resp.url)
        if resp.status >= 400:
            stats[h]["bad"] += 1
            stats[h]["err"] = stats[h]["err"] or f"HTTP {resp.status}"
            if CRITICAL in resp.url:
                critical["outcome"] = f"HTTP {resp.status}"
            if once(f"http:{h}:{resp.status}"):
                print(f"{stamp()} HTTP {resp.status}  {resp.url[:110]}")
        else:
            stats[h]["ok"] += 1
            if CRITICAL in resp.url:
                critical["outcome"] = f"HTTP {resp.status} OK"
            if VERBOSE:
                print(f"{stamp()} RESP   {resp.status} {resp.url[:115]}")

    def on_requestfailed(req):
        h = _host(req.url)
        failure = _short(req.failure or "unknown", 60)
        stats[h]["bad"] += 1
        stats[h]["err"] = stats[h]["err"] or failure
        if CRITICAL in req.url:
            critical["outcome"] = failure
        if once(f"fail:{h}:{failure}"):
            print(f"{stamp()} FAILED {failure:45} {h}")

    def on_console(msg):
        if msg.type != "error" or MUTE.search(msg.text):
            return
        text = _short(msg.text)
        if once(f"console:{text[:80]}"):
            print(f"{stamp()} CONSOLE {text}")

    def on_pageerror(err):
        text = _short(err)
        if once(f"pageerror:{text[:80]}"):
            print(f"{stamp()} PAGEERROR {text}")

    handlers = [
        ("request", on_request), ("response", on_response),
        ("requestfailed", on_requestfailed), ("console", on_console),
        ("pageerror", on_pageerror),
    ]
    for event, fn in handlers:
        page.on(event, fn)

    yield

    for event, fn in handlers:
        try:
            page.remove_listener(event, fn)
        except Exception:
            pass

    # ---- summary ---------------------------------------------------------
    broken = sorted((h for h in stats if stats[h]["bad"]), key=lambda h: -stats[h]["bad"])
    healthy = [h for h in stats if not stats[h]["bad"]]

    print(f"\n{'=' * 78}\nNETDEBUG  {request.node.name}   ({time.time() - t0:.1f}s)")

    if broken:
        print(f"\n  hosts with failures ({len(broken)}):")
        print(f"    {'host':45} {'req':>5} {'bad':>5}  first error")
        for h in broken:
            s = stats[h]
            print(f"    {h[:45]:45} {s['req']:>5} {s['bad']:>5}  {s['err']}")
    else:
        print("\n  no request failures")

    if healthy:
        total = sum(stats[h]["req"] for h in healthy)
        print(f"\n  {len(healthy)} host(s) fully healthy, {total} requests")

    # The question the test3-vs-uat2 comparison turns on.
    if critical["requested"]:
        print(f"\n  {CRITICAL}: REQUESTED -> {critical['outcome'] or 'no response seen'}")
    else:
        print(f"\n  {CRITICAL}: NEVER REQUESTED by this environment")

    repeats = {k: n for k, n in seen.items() if n > 1}
    if repeats:
        print(f"\n  {len(repeats)} problem(s) repeated; top:")
        for k, n in sorted(repeats.items(), key=lambda kv: -kv[1])[:5]:
            print(f"    x{n:<5} {k[:100]}")
    print("=" * 78)


@pytest.fixture(autouse=True)
def playwright_trace(page, request):
    """Opt-in Playwright trace. Set NETDEBUG_TRACE=1.

    View with:  .venv/bin/playwright show-trace test-results/<name>-trace.zip
    Tracing is used rather than record_har_path because HAR needs a context
    Playwright created itself, which is not the case over connect_over_cdp.
    """
    if not TRACE:
        yield
        return
    context = page.context
    out = f"test-results/{request.node.name}-trace.zip"
    try:
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
    except Exception as exc:
        print(f"tracing not started: {exc}")
        yield
        return
    yield
    try:
        context.tracing.stop(path=out)
        print(f"TRACE: .venv/bin/playwright show-trace {out}")
    except Exception as exc:
        print(f"tracing not saved: {exc}")
