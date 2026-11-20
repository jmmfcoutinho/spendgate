"""The scriptable fake upstream as a standalone ASGI app, for the container in compose.test.yaml.

`tests.fakes.FakeUpstream` already speaks every provider's wire shape and logs what reaches it. This
wrapper only adds two control routes so a test outside the container can read the log. It lives
here, not in tests/fakes, so the fake's semantics stay untouched.
"""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from tests.fakes import FakeUpstream

fake = FakeUpstream()


async def log(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "count": fake.calls,
            "requests": [{"method": r.method, "path": r.path} for r in fake.requests],
        }
    )


async def reset(_request: Request) -> JSONResponse:
    fake.requests.clear()
    return JSONResponse({"count": 0})


# The control paths cannot collide with a provider path: none starts with `__`.
app = Starlette(
    routes=[
        Route("/__log", log, methods=["GET"]),
        Route("/__reset", reset, methods=["POST"]),
        Mount("/", app=fake.app),
    ]
)
