# ADR-0001: Ports and adapters

**Status:** Proposed
**Date:** 2026-11-13

## Context

spendgate must support many providers and two databases, and an auditor must be able to read the money rules without reading I/O code. LiteLLM mixes provider code, routing and budgets in huge modules. We want the opposite.

## Decision

Use ports and adapters. `core` holds pure rules and defines value types. `ledger.port` and `providers.port` are the interfaces. SQLite, Postgres and each provider are adapters that import `core` only. `spendgate.pipeline` (steps, routing, streaming relay, key verification; no HTTP framework) is the shared wiring layer; `gateway` (HTTP) and `local` (in-process) sit above it as siblings and never import each other. Layers: `cli` > `gateway` | `local` > `pipeline` > `ledger`, `providers`, `config`, `client` > `core`. import-linter enforces this in CI (see architecture.md section 4).

## Decision drivers

- A new provider is one adapter module and no pipeline change (success criterion).
- Money rules testable with no network and no database.
- Small, readable files.

## Alternatives considered

1. One flat package with a few large modules.
2. Plugin system with entry points and dynamic discovery.
3. Full hexagonal framework with dependency-injection container.

## Rationale

Plain modules plus explicit wiring in one place is the smallest structure that gives enforceable boundaries. Dynamic plugins hide what runs, which hurts auditability.

## Consequences

### Positive

- Auditors read `core` and the ledger port first.
- Adapters are replaceable and testable with fakes.

### Negative

- A little more wiring code and a few extra type definitions.

### Risks and follow-up

- The pipeline package must stay free of Starlette and uvicorn; the contract fails the build otherwise (it lives outside `gateway`).

## Related documents

- Requirements: a small, auditable code base and enforced quality gates
- Architecture: architecture.md
- Supersedes: none
