"""A stand-in app on the `apps` network: the guard answers, the internet does not.

Standard library only. It holds no provider key. Optional: SPENDGATE_API_KEY, a virtual key, makes
it send one authenticated request through the guard.
"""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

GUARD = os.environ.get("SPENDGATE_URL", "http://spendgate:8080")
HTTP_OK = 200


def get(path: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    # Only the guard's own http URL is ever opened here, never a caller-supplied scheme.
    req = urllib.request.Request(GUARD + path, headers=headers or {})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def main() -> int:
    for _ in range(30):  # compose waits for the health check; this covers `docker compose run`
        try:
            status, _body = get("/readyz")
            if status == HTTP_OK:
                break
        except OSError:
            pass
        time.sleep(1)
    else:
        print("FAIL: the guard did not become ready")
        return 1
    print(f"guard   {GUARD}/readyz -> 200")

    status, body = get("/v1/models")
    # Refused by the guard itself: no upstream call is made.
    print(f"guard   no key -> {status} {body.strip()[:80]}")

    key = os.environ.get("SPENDGATE_API_KEY")
    if key:
        status, body = get("/v1/models", {"authorization": f"Bearer {key}"})
        print(f"guard   with SPENDGATE_API_KEY -> {status} {json.dumps(body[:80])}")

    try:
        addr = socket.getaddrinfo("api.openai.com", 443)[0][4][0]
        print(f"FAIL: api.openai.com resolved to {addr}; the perimeter is open")
        return 1
    except socket.gaierror as exc:
        print(f"direct  api.openai.com does not resolve ({exc.strerror}): the perimeter holds")
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        print("FAIL: reached 1.1.1.1:443 directly; the perimeter is open")
        return 1
    except OSError as exc:
        print(f"direct  1.1.1.1:443 unreachable ({exc}): the perimeter holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
