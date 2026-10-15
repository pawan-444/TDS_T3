
import contextvars
import hashlib
import os
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount


EMAIL = "23f2002204@ds.study.iitm.ac.in".strip().lower()

# The grader sends this header with each tool call.
request_headers = contextvars.ContextVar("request_headers", default={})

security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False,
)

mcp = FastMCP(
    "Exam Challenge Server",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=security,
)


@mcp.tool()
def solve_challenge(ctx: Context) -> str:
    """Calculate the required hash using the current X-Exam-Challenge header."""
    headers = request_headers.get({})
    challenge = headers.get("x-exam-challenge")

    if not challenge:
        raise ValueError("Missing X-Exam-Challenge header")

    message = f"{challenge}:{EMAIL}"
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]


class HeaderCaptureMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

        token = request_headers.set(headers)
        try:
            await self.app(scope, receive, send)
        finally:
            request_headers.reset(token)


@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[
        Mount("/", app=mcp.streamable_http_app()),
    ],
    lifespan=lifespan,
)

app.add_middleware(HeaderCaptureMiddleware)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )
