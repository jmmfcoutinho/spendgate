# ADR-0002: Starlette over FastAPI

**Status:** Proposed
**Date:** 2026-11-13

## Context

The gateway proxies and streams requests, mostly passing bodies through. It needs routing, middleware, streaming responses and a test client. It does not need generated API docs or request-model magic on the hot path. Base install is limited to 6 runtime dependencies.

## Decision

Build the gateway on Starlette, served by uvicorn. Use Pydantic only for config and admin API payloads, not for proxied bodies. Use httpx for upstream calls.

## Decision drivers

- Size and dependency count (FastAPI adds its own layer on top of Starlette).
- Control over body reading: size and concurrency checks must run before the body is read.
- Control over streaming and cancellation.
- Auditability: fewer layers between request and ledger.

## Alternatives considered

1. FastAPI: nicer admin docs, but dependency injection and body parsing sit between us and the raw request.
2. aiohttp or plain ASGI: fewer features to rely on, more code to own.
3. Litestar: capable, but smaller community and a new dependency tree.

## Rationale

Starlette is what FastAPI uses underneath, is small and stable, and gives exact control of the request lifecycle. The admin API is small enough to write by hand with a short OpenAPI file.

## Consequences

### Positive

- One fewer layer to audit. Precise pre-body checks.
- Fewer dependencies and a smaller image.

### Negative

- No automatic OpenAPI docs for admin routes. We write them.
- Contributors who know FastAPI need a short ramp.

### Risks and follow-up

- Starlette is pre-1.0 in some lines. Pin a compatible range and run CI against the latest.

## Related documents

- Requirements: at most 6 base runtime dependencies
- Architecture: architecture.md sections 2 and 5
- Supersedes: none
