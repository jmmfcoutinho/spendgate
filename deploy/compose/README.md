# Reference Compose with a perimeter

Apps reach models only through spendgate. Run from this directory (or pass `-f deploy/compose/compose.yaml`).

```
apps   (internal)  demo app(s) + spendgate        no route to the internet
egress             spendgate only                 the one container that can reach providers
db     (internal)  spendgate + postgres           --profile postgres only
```

## From zero

```bash
cd deploy/compose
umask 077 && mkdir -p secrets
# Real provider keys. The key pepper is any long random string (keep it: changing it voids every key).
printf '%s' "$OPENAI_API_KEY"    > secrets/openai_api_key
printf '%s' "$ANTHROPIC_API_KEY" > secrets/anthropic_api_key
openssl rand -hex 32 | tr -d '\n' > secrets/spendgate_key_pepper
: > secrets/spendgate_database_url; : > secrets/postgres_password   # placeholders unless you use Postgres
# Compose bind-mounts the files as they are, and the container runs as UID 10001, so the files must be
# world-readable; the 0700 directory is what keeps other host users out.
chmod 700 secrets && chmod 644 secrets/*

docker compose build                         # builds spendgate:core
docker compose run --rm spendgate migrate    # creates the ledger (and upgrades it after a new release)
docker compose up -d                         # waits until the guard is healthy
docker compose logs demo            # the demo app: guard answers, internet does not

# Create a key (the secret is printed once) and call the guard with it from an app container.
docker compose exec spendgate spendgate keys create --local --scope org:acme/project:app --name my-app
docker compose run --rm -e SPENDGATE_API_KEY=sg-... demo

docker compose down -v              # stop and delete containers, networks and the ledger volume
```

`--local` talks to the ledger directly, so no admin key is needed; run the CLI only through `docker compose exec`.
Other commands: `spendgate usage --local`, `budgets list <scope> --local`, `kill on --local --reason ...`.
Budgets and scopes come from `bootstrap:` in the config (create-only); edit `spendgate.sqlite.yaml` and `up -d` again.
Keys under `bootstrap.keys` are created only by `docker compose run --rm spendgate migrate --bootstrap`, which prints each secret once when run from a terminal; without one (`-T`, CI) add `--secrets-file` with a path on a mounted volume ([operations](../../docs/operations.md#keys), ADR-0032).

## Postgres

The `full` image carries the Postgres driver; the config differs only in `ledger.backend`.

```bash
printf '%s' "$(openssl rand -hex 16)" > secrets/postgres_password
printf 'postgresql://spendgate:%s@db:5432/spendgate' "$(cat secrets/postgres_password)" > secrets/spendgate_database_url  # pragma: allowlist secret
chmod 644 secrets/*
export SPENDGATE_VARIANT=full SPENDGATE_LEDGER=postgres
docker compose --profile postgres build
docker compose --profile postgres run --rm spendgate migrate   # starts db, migrates, exits
docker compose --profile postgres up -d
```

`--profile postgres` adds the `db` service; `SPENDGATE_LEDGER=postgres` selects `spendgate.postgres.yaml`.
Set both together. (Compose cannot switch a service's config by profile alone.)

## Upgrading

Migrating is a separate step: `serve` on Postgres refuses a schema that is behind and says so. After
pulling or building a new image, run `docker compose run --rm spendgate migrate` (add
`--profile postgres` for Postgres), then `docker compose up -d`. The migrate container is a one-off of
the `spendgate` service, so it gets the same secrets and networks and is deleted when it exits. Before a
release that changes the schema, back up the ledger. Rollbacks and the compatibility rules are in
[Upgrades and rollbacks](../../docs/operations.md#upgrades-and-rollbacks).

## Rotating a key or password

Overwrite the secret file in place (the container sees the bind-mounted file, not a new one renamed over
it), then tell the guard to read its `file:` secrets again; no restart, calls in flight are not cut:

```bash
printf '%s' "$NEW_OPENAI_API_KEY" > secrets/openai_api_key
docker compose kill -s HUP spendgate
docker compose logs spendgate | grep secrets_reloaded    # the paths read and changed, never a value
```

The database URL reloads the same way, but only its password may change: rotate the role's password
in Postgres first (`ALTER ROLE spendgate PASSWORD ...`), then write the new URL. Connections already
open stay logged in. Details: [rotating secrets](../../docs/operations.md#rotating-secrets).

## Admin API on its own network

By default the admin API listens on 127.0.0.1 inside the guard's container: only `docker compose exec`
reaches it. To call it from another container (an operator's CLI, a Prometheus scraper for `/metrics`)
without opening it to the apps, add `compose.admin.yaml`. It puts the guard on an internal `admin`
network under the name `spendgate-admin`, which exists on that network only, and adds an `operator`
service that joins `admin` and nothing else:

```
admin  (internal)  spendgate + operator           apps are not on it; no route to the internet
```

Bind the admin listener to that name in the config (`spendgate.sqlite.yaml` or `spendgate.postgres.yaml`):

```yaml
server:
  admin: { listen: spendgate-admin:8081 }   # the guard's address on `admin`, resolved once at start
```

Then pass both files on every command (or set `COMPOSE_FILE=compose.yaml:compose.admin.yaml`):

```bash
docker compose -f compose.yaml -f compose.admin.yaml up -d
export SPENDGATE_ADMIN_KEY=sga-...    # an admin key (bootstrap `admin_scopes`, `migrate --bootstrap`)
docker compose -f compose.yaml -f compose.admin.yaml run --rm -e SPENDGATE_ADMIN_KEY operator status
```

The operator service runs the `spendgate` CLI with `SPENDGATE_URL=http://spendgate-admin:8081` and
starts only with `run` (profile `operator`). A service of your own reaches the admin API by joining
`admin`; one that also joins `apps` makes the separation moot. `/metrics` on this port needs no key, so
whatever joins `admin` can read it.

Never use `0.0.0.0` for the admin listener here: it binds every interface of the guard, including
the one on `apps`. Forgetting the overlay fails closed: the name does not resolve, so the admin
listener fails to start rather than falling back to another address. Inside the guard's container, `docker compose exec spendgate spendgate ... --local`
still works; the HTTP commands there need `--url http://spendgate-admin:8081`, because the CLI
takes an admin address from the config only on loopback. The key travels in plain HTTP on the
`admin` bridge, which only the guard and the operator join.

**Kubernetes.** A pod has one network interface, so binding cannot separate the ports there. Keep
`server.admin.listen: 0.0.0.0:8081` and let a NetworkPolicy admit port 8081 only from the operator's
and the scraper's pods. This is not yet a tested pattern (see the [roadmap](../../ROADMAP.md)).

## What the perimeter guarantees

- An app container on `apps` cannot resolve or connect to any provider (or any internet host). It holds no
  provider key, pepper or database URL: those are Docker secrets mounted only into `spendgate`.
- Every service is `read_only`, `cap_drop: [ALL]`, `no-new-privileges`, with memory and pids limits and a
  tmpfs `/tmp`. Neither spendgate port is published on the host. The admin API listens on 127.0.0.1 inside
  the container, so apps cannot reach it either. With `compose.admin.yaml` it listens on the `admin`
  network only: the operator reaches it, an app on `apps` cannot, by name or by address.
- An app that holds its own leaked provider key still cannot use it from `apps`.

## What it does not

- R-01 (see the [roadmap](../../ROADMAP.md)): spendgate itself can reach any internet host; there is no egress allowlist. Hosts come
  from the config, and body URLs are never fetched, but a compromised gateway could exfiltrate.
- An app you attach to `egress` (or run with `network_mode: host`) is outside the perimeter. Keep apps on `apps` only.
- Publishing a port to the host (see the comments in `compose.yaml`) lets host processes call the guard.
- It is a single-host setup: a kernel or Docker daemon compromise defeats it (R-02).

Perimeter tests will assert the guarantees above against this file.
