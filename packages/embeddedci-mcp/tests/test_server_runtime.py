"""Server behaviour around the tools: event-loop offload, idle lease release, HTTP auth, CLI, and
the frozen tool surface."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import anyio
import pytest

import embeddedci_mcp.session as session_mod
from embeddedci_mcp import __main__ as cli
from embeddedci_mcp.http_auth import BearerTokenMiddleware
from embeddedci_mcp.server import mcp
from embeddedci_mcp.session import SESSION, Session

from conftest import FakeTransport

SURFACE = Path(__file__).with_name("tools_surface.json")


def test_slow_tools_do_not_block_the_event_loop(connected, monkeypatch):
    pod = SESSION.require()
    real_status = pod.status

    def slow_status():
        time.sleep(0.4)
        return real_status()

    monkeypatch.setattr(pod, "status", slow_status)
    ticks = 0

    async def main():
        nonlocal ticks

        async def ticker():
            nonlocal ticks
            for _ in range(20):
                await anyio.sleep(0.02)
                ticks += 1

        async with anyio.create_task_group() as tg:
            tg.start_soon(ticker)
            await mcp.call_tool("status", {})

    anyio.run(main)
    assert ticks >= 10  # the ticker kept running while status slept in a worker thread


class _LeasedPod:
    """A stand-in pod that holds a lease and records closes."""

    leased = True

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_idle_cloud_connection_releases_and_reconnects(monkeypatch):
    opened = []

    def factory(connection, **kwargs):
        pod = _LeasedPod()
        opened.append(pod)
        return pod

    monkeypatch.setattr(session_mod, "BenchPod", factory)
    s = Session()
    s.idle_timeout = 0.1
    s.connect("embeddedci:bench-1")
    time.sleep(0.4)
    assert opened[0].closed and not s.connected and s.reconnectable
    pod = s.require()  # the next tool call reconnects
    assert pod is opened[1] and s.connected
    s.disconnect()
    assert opened[1].closed and not s.reconnectable


def test_local_connections_never_idle_release(monkeypatch):
    class Local(_LeasedPod):
        leased = False

    monkeypatch.setattr(session_mod, "BenchPod", lambda c, **k: Local())
    s = Session()
    s.idle_timeout = 0.05
    pod = s.connect("192.168.1.2")
    time.sleep(0.2)
    assert s.connected and not pod.closed
    s.disconnect()


def test_bearer_token_middleware():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    app = Starlette(routes=[Route("/mcp", lambda request: PlainTextResponse("ok"))])
    client = TestClient(BearerTokenMiddleware(app, "s3cret"))
    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer s3cret"}).text == "ok"
    with pytest.raises(ValueError):
        BearerTokenMiddleware(app, "")


def test_cli_refuses_an_open_network_bind(monkeypatch):
    monkeypatch.delenv(cli.TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit):
        cli.main(["--transport", "http", "--host", "0.0.0.0"])


def test_cli_loopback_detection():
    assert cli._is_loopback("127.0.0.1") and cli._is_loopback("localhost") and cli._is_loopback("::1")
    assert not cli._is_loopback("0.0.0.0") and not cli._is_loopback("192.168.1.10")


def _render_surface():
    tools = anyio.run(mcp.list_tools)
    return {t.name: {"input": t.inputSchema,
                     "annotations": t.annotations.model_dump(exclude_none=True) if t.annotations else None}
            for t in sorted(tools, key=lambda t: t.name)}


def test_tool_surface_matches_snapshot():
    """Freeze tool names, input schemas and annotations. Refresh deliberately with
    UPDATE_TOOLS_SURFACE=1 after checking the change is compatible for existing agents."""
    current = _render_surface()
    if os.environ.get("UPDATE_TOOLS_SURFACE"):
        SURFACE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    expected = json.loads(SURFACE.read_text())
    changed = sorted(k for k in set(current) | set(expected) if current.get(k) != expected.get(k))
    assert not changed, "tool surface changed for: " + ", ".join(changed)


def test_fake_transport_is_importable():
    assert FakeTransport().ping() == "pong"
