"""Streamable HTTP transport for the Google Analytics MCP server.

Replaces the legacy HTTP+SSE transport (previously `sse_server.py`). Claude's
remote connector support lists Streamable HTTP as the standard transport and
is deprecating HTTP+SSE; this module exposes a single `/mcp` endpoint that
implements it.

Runs `stateless=True` deliberately: Cloud Run autoscales this service across
multiple instances with no session affinity, so any per-request state must be
self-contained rather than kept in server memory keyed by a session id.
"""

import contextlib
import os

import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

import analytics_mcp.coordinator as coordinator

# json_response=True returns a single JSON body per request instead of an
# SSE-framed stream, which is simpler to reason about behind Cloud Run's
# proxy and avoids partial/truncated response bodies on short-lived calls.
#
# security_settings is left unset (None) on purpose: TransportSecurityMiddleware
# only disables DNS-rebinding Host/Origin checks when passed None. Passing a
# default-constructed TransportSecuritySettings() would enable those checks
# with an empty allowed_hosts list and reject every request with 421, since
# Cloud Run's proxy presents its own Host header.
session_manager = StreamableHTTPSessionManager(
    app=coordinator.app,
    json_response=True,
    stateless=True,
)


async def health(request):
    return PlainTextResponse("ok")


class StreamableHTTPASGIApp:
    """Thin ASGI wrapper so Starlette routes to `handle_request` untouched.

    `Route(endpoint=...)` wraps plain functions/bound methods with
    `request_response()`, which expects `endpoint(request) -> Response` and
    would break on `handle_request`'s raw `(scope, receive, send) -> None`
    signature. A wrapper *instance* (not a function or method) makes
    Starlette treat it as a raw ASGI app and call it directly instead --
    the same pattern the MCP SDK's own FastMCP server uses.

    A raw ASGI app is also what a `Mount` needs, but `Mount` always compiles
    its path regex as `<path>/(?P<path>.*)`, so it can never match a bare
    `/mcp` request -- only `/mcp/...`. A `Route` matches the exact path, so
    the URL users paste into a connector works without a redirect.
    """

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope, receive, send) -> None:
        await self._session_manager.handle_request(scope, receive, send)


mcp_app = StreamableHTTPASGIApp(session_manager)


@contextlib.asynccontextmanager
async def lifespan(app):
    # StreamableHTTPSessionManager.run() may only be called once per
    # instance; it must stay active for the lifetime of the ASGI app.
    async with session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/", health),
        Route("/healthz", health),
        Route("/mcp", endpoint=mcp_app, methods=["GET", "POST", "DELETE"]),
    ],
    lifespan=lifespan,
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
