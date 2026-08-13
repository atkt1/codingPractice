"""Network/console instrumentation to root-cause the D365 clientsdkproxy script error.

Paste into field/tests/conftest.py (or import it there). Autouse, so every test in
scope gets it. Read-only — it observes, it never blocks or rewrites a request.

The question it answers: when the "The script .../clientsdkproxy?version=1 didn't
load correctly" dialog appears, WHAT happened to that request — was it aborted,
refused, unauthorized, blocked, slow, or never sent at all?

Run one failing test with -s so the output is not captured:

    .venv/bin/python -m pytest field/tests/web/test_life_events_family_goal.py \
        -v -s --no-header --dist=no -p no:allure_pytest --import-mode=importlib -o addopts=
"""

import time

import pytest

# Anything whose absence breaks the D365 client API. Add to this list, do not narrow it:
# the interesting finding is often a request you did not expect to matter.
WATCH = (
    "clientsdkproxy",
    "apps.powerapps.com",
    "content.powerapps.com",
    "/webresources/",
    "ClientApiWrapper",
    "main.aspx",
)


def _watched(url: str) -> bool:
    return any(token in url for token in WATCH)


@pytest.fixture(autouse=True)
def netdebug(page):
    """Print the lifecycle of every script the D365 client API depends on."""
    t0 = time.time()
    seen = {}

    def stamp() -> str:
        return f"[{time.time() - t0:7.2f}s]"

    def on_request(req):
        if _watched(req.url):
            seen[req.url] = time.time()
            print(f"{stamp()} REQ    {req.resource_type:10} {req.url[:130]}")

    def on_response(resp):
        if _watched(resp.url):
            started = seen.get(resp.url)
            took = f"{(time.time() - started) * 1000:.0f}ms" if started else "?"
            marker = "  <<< NOT OK" if resp.status >= 400 else ""
            print(f"{stamp()} RESP   {resp.status} in {took:>8} {resp.url[:120]}{marker}")

    def on_requestfailed(req):
        # req.failure is the raw Chromium net error, e.g. net::ERR_ABORTED.
        # This is the single most diagnostic line in the whole fixture.
        print(f"{stamp()} FAILED {req.failure!s:32} {req.url[:120]}")

    def on_console(msg):
        if msg.type in ("error", "warning"):
            print(f"{stamp()} CONSOLE {msg.type.upper()}: {msg.text[:300]}")

    def on_pageerror(err):
        print(f"{stamp()} PAGEERROR: {str(err)[:400]}")

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_requestfailed)
    page.on("console", on_console)
    page.on("pageerror", on_pageerror)

    yield

    # Cross-origin auth state at the end of the test. A manual browser has usually
    # accumulated cookies for apps.powerapps.com; a fresh automation context has not,
    # and a cross-site script request without them can be refused or redirected.
    try:
        for origin in ("https://apps.powerapps.com", "https://fieldcrm-uat2.crm.dynamics.com"):
            cookies = page.context.cookies(origin)
            names = sorted(c["name"] for c in cookies)
            print(f"\nCOOKIES {origin}: {len(cookies)} -> {names[:12]}")
    except Exception as exc:                                     # context may be closed
        print(f"\nCOOKIES unavailable: {exc}")

    page.remove_listener("request", on_request)
    page.remove_listener("response", on_response)
    page.remove_listener("requestfailed", on_requestfailed)
    page.remove_listener("console", on_console)
    page.remove_listener("pageerror", on_pageerror)


@pytest.fixture(autouse=True)
def playwright_trace(page, request):
    """Full trace: network, DOM snapshots, and the exact action timing.

    View it with:  .venv/bin/playwright show-trace test-results/<name>-trace.zip

    Works on a CDP-attached context too, unlike record_har_path, which needs a
    context that Playwright created itself.
    """
    context = page.context
    out = f"test-results/{request.node.name}-trace.zip"
    try:
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
    except Exception as exc:                       # already tracing, or unsupported
        print(f"tracing not started: {exc}")
        yield
        return
    yield
    try:
        context.tracing.stop(path=out)
        print(f"\nTRACE: {out}   ->  .venv/bin/playwright show-trace {out}")
    except Exception as exc:
        print(f"tracing not saved: {exc}")
