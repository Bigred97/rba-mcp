"""Shared fixtures.

Resets the server's module-level RBAClient between tests so a closed event
loop from one test doesn't poison the next one. The reset is best-effort —
during early build, server.py may not exist yet.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _reset_server_client():
    yield
    try:
        from rba_mcp import server
    except ImportError:
        return
    if hasattr(server, "reset_client_for_tests"):
        await server.reset_client_for_tests()
