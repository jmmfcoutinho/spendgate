"""The fake provider as a real local HTTP server (one process), driven by the plan in `user`.

Run: `python -m tests.load.fake_server PORT`. It reuses `tests.fakes.FakeUpstream` for every wire
shape and only replaces the request handler, so a test can ask for real latency, an error status
or a mid-stream cut per request. `GET /_load/stats` returns the upstream contact counters.
"""

import asyncio
import json
import sys
from collections import Counter
from collections.abc import AsyncIterator

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tests.fakes import FakeUpstream, FakeUsage, Script
from tests.fakes.script import DEFAULT_USAGE
from tests.fakes.shape_types import Ctx
from tests.fakes.shapes import shape_for
from tests.load.plan import Plan

INPUT_TOKENS = DEFAULT_USAGE.input_tokens


class LoadUpstream(FakeUpstream):
    def __init__(self) -> None:
        super().__init__()
        self.total = 0
        self.per_key: Counter[int] = Counter()
        self.statuses: Counter[int] = Counter()
        self.app.router.routes.insert(0, Route("/_load/stats", self._stats))

    async def _stats(self, _request: Request) -> Response:
        return JSONResponse(
            {
                "total": self.total,
                "per_key": {str(k): v for k, v in self.per_key.items()},
                "statuses": {str(k): v for k, v in self.statuses.items()},
            }
        )

    async def _handle(self, request: Request) -> Response:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        plan = Plan.decode(body.get("user", ""))
        self.total += 1
        self.per_key[plan.key] += 1
        self.statuses[plan.status] += 1
        if plan.latency_ms:
            await asyncio.sleep(plan.latency_ms / 1000)
        shape = shape_for(request.url.path)
        if shape is None:
            return Response(b'{"error":"unknown path"}', 404, media_type="application/json")
        out = plan.out_tokens * (3 if plan.overreport else 1)
        script = Script(
            usage=FakeUsage(input_tokens=INPUT_TOKENS, output_tokens=out),
            status=plan.status,
            cut_after_frames=plan.cut_after,
        )
        ctx = Ctx(script, body, request.url.path, dict(request.query_params))
        return await self._answer(shape, ctx)

    async def _emit(self, frames: list[bytes], script: Script) -> AsyncIterator[bytes]:
        for frame in frames:
            yield frame
        if script.cut_after_frames is not None:
            # Raising after the headers went out makes uvicorn drop the connection mid-body.
            raise ConnectionResetError("fake provider cut the stream")


def main() -> None:
    port = int(sys.argv[1])
    config = uvicorn.Config(
        LoadUpstream().app, "127.0.0.1", port, log_level="critical", access_log=False,
        backlog=2048,
    )  # fmt: skip
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
