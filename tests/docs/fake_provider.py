"""The fake provider as a real local HTTP server, for doc tests that start `spendgate serve`.

    python -m tests.docs.fake_provider PORT

It answers the OpenAI and Anthropic paths with canned replies and checks no credential. It binds
127.0.0.1 only.
"""

import sys

import uvicorn

from tests.fakes import FakeUpstream


def main(port: int) -> None:
    uvicorn.run(FakeUpstream().app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main(int(sys.argv[1]))
