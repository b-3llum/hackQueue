from __future__ import annotations

import asyncio

from aioresponses import aioresponses

URL = "https://example.test/api/thing"


async def test_parses_json(http):
    with aioresponses() as m:
        m.get(URL, payload={"ok": True})
        result = await http.get(URL)
    assert result.status == 200
    assert result.is_json
    assert result.data == {"ok": True}


async def test_retries_5xx_then_succeeds(http):
    with aioresponses() as m:
        m.get(URL, status=502)
        m.get(URL, payload={"ok": 1})
        result = await http.get(URL)
    assert result.status == 200


async def test_gives_up_after_max_retries(http):
    with aioresponses() as m:
        m.get(URL, status=503, repeat=True)
        result = await http.get(URL, max_retries=2)
    assert result.status == 503


async def test_does_not_follow_redirects(http):
    # HTB signals auth failure with a 302 to an HTML login page — the client
    # must hand back the 302, never the login page.
    with aioresponses() as m:
        m.get(URL, status=302, headers={"Location": "https://example.test/login"})
        result = await http.get(URL)
    assert result.status == 302


async def test_vercel_challenge_is_not_retried(http):
    with aioresponses() as m:
        m.get(
            URL,
            status=429,
            headers={"x-vercel-mitigated": "challenge", "Content-Type": "text/html"},
            body="<html>checkpoint</html>",
        )
        # Only ONE mock registered: a retry would raise a no-match error.
        result = await http.get(URL)
    assert result.status == 429
    assert result.is_challenge
    assert not result.is_json


async def test_html_429_counts_as_challenge(http):
    with aioresponses() as m:
        m.get(URL, status=429, headers={"Content-Type": "text/html"}, body="<html></html>")
        result = await http.get(URL)
    assert result.is_challenge


async def test_json_429_is_not_a_challenge_and_retries(http):
    with aioresponses() as m:
        m.get(URL, status=429, headers={"Content-Type": "application/json"}, body="{}")
        m.get(URL, payload={"ok": 1})
        result = await http.get(URL)
    assert result.status == 200


async def test_per_host_spacing():
    from hackqueue.http.client import _HostSpacing

    spacing = _HostSpacing()
    loop = asyncio.get_running_loop()
    start = loop.time()
    await spacing.wait("h", 0.05)
    await spacing.wait("h", 0.05)
    assert loop.time() - start >= 0.045


async def test_spacing_is_per_host_not_global():
    from hackqueue.http.client import _HostSpacing

    spacing = _HostSpacing()
    loop = asyncio.get_running_loop()
    start = loop.time()
    await spacing.wait("a", 0.5)
    await spacing.wait("b", 0.5)  # different host: no wait
    assert loop.time() - start < 0.3
