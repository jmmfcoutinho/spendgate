# ADR-0027: The admin API on its own Compose network

**Status:** Accepted
**Date:** 2026-11-25

## Context

Issue #11. The reference Compose stack keeps the admin API (`server.admin.listen: 127.0.0.1:8081`)
on loopback inside the guard's container, so only `docker compose exec` reaches it. An operator
who wants it in another container (the admin CLI, a Prometheus scraper for `/metrics`) had one
documented way: bind `0.0.0.0:8081`. That binds every interface of the guard, including the one on
`apps`, so every app container can open a TCP connection to the admin port; only the admin keys
protect it, and `/metrics` there needs no key at all.

`serve` already binds the admin host exactly as configured (uvicorn receives it unchanged), and
the schema accepts a host name as well as an address. A container joins one interface per network,
so binding the guard's address on a dedicated network is enough to keep the port off `apps`.

## Decision

- A new optional overlay, `deploy/compose/compose.admin.yaml`, adds an internal `admin` network.
  The guard joins it under the alias `spendgate-admin`, which exists on that network only, and an
  `operator` service (the spendgate image running the CLI, profile `operator`, no secrets,
  `SPENDGATE_URL=http://spendgate-admin:8081`) joins `admin` and nothing else.
- With the overlay, the config sets `server.admin.listen: spendgate-admin:8081`. The name is
  resolved once at start and resolves to the guard's address on `admin`. A name, not a fixed
  address: a fixed subnet would collide with other networks on the host and between projects.
  Without the overlay the name does not resolve and the admin listener does not start, so a
  missing overlay fails closed instead of falling back to a wider address.
- The default stays as it is: admin on loopback inside the container. On a network the port can
  also be reached from the host through the bridge, and from anything later attached to `admin`;
  only operators who need it should take that on.
- A perimeter test (T-ADM-03, `tests/perimeter/test_admin_network.py`) runs the stack with the
  overlay and checks that an app on `apps` cannot connect to the admin port by the guard's name,
  the admin name, or either address, that the operator can, and that the guard does not listen on
  loopback for it.

## Consequences

- No code, schema or default changes. One new shipped file and a documented config line.
- With the overlay, the HTTP admin commands run through `docker compose exec spendgate` need
  `--url http://spendgate-admin:8081` (the CLI takes an admin address from the config only on
  loopback); `--local` is unchanged.
- The admin key crosses the `admin` bridge in plain HTTP. Only the guard and the operator join it;
  a service that also joins `apps` undoes the separation, and the docs say so.
- Kubernetes gets a short note only (one interface per pod, so a NetworkPolicy does the job); a
  tested pattern stays on the roadmap.
