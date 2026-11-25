# Running spendgate locally

Three levels of protection. Each says what it stops and what it does not.

| Level | Protects against | Does not protect against |
|-------|------------------|--------------------------|
| SDK only (library mode) | an honest app overspending | an app that skips the SDK (cooperative) |
| Local proxy (`spendgate serve`) | any client that uses the proxy URL | a client that calls the provider directly |
| Compose with a perimeter | apps calling providers at all, except through the guard | a compromised guard itself (it can still reach any host) |

Each step ends with a command that proves it works. The code blocks marked `doctest` in this page are run
by `tests/docs/test_local_guide.py` against a fake provider, so a command that drifts from the code fails
CI. Nothing here needs a real provider key to follow the logic; real keys are only for real calls.

## Step 1: SDK only (library mode)

**What you get:** a hard spending cap inside one script. No server, no keys to hand out; a SQLite file
remembers what was spent.

Install the package from PyPI (Python 3.13 or newer) with the SDK you use:

```bash
pip install "spendgate[openai]"         # or: uv pip install "spendgate[openai]"
export OPENAI_API_KEY=sk-...            # your real key
```

The commands in steps 2 and 3 run from a checkout of this repository, because they use its example
config and Compose files. Get one with:

```bash
git clone https://github.com/jmmfcoutinho/spendgate && cd spendgate
uv sync --extra openai
```

With the package installed from PyPI instead, drop the `uv run` prefix, or run the CLI without
installing it with `uvx spendgate ...`.

Save as `guard_demo.py`. The cap is tiny so you see the refusal at once:

<!-- doctest: step1 -->
```python
import spendgate.client as sg_client
import spendgate.local as sg

guard = sg.guard(cap="0.01 USD", ledger_path="guide-ledger.db")
client = guard.openai()
ask = {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "hi"}]}

client.chat.completions.create(max_tokens=20, **ask)
print("small call: allowed")

try:
    with sg_client.raise_for_spendgate():  # turns the SDK's 402 into a typed error
        client.chat.completions.create(max_tokens=100_000, **ask)  # worst case costs 0.05 USD
except sg_client.BudgetExhausted as exc:
    print("big call: refused", exc.code)
```

**Verify:** run `python guard_demo.py` (`uv run python guard_demo.py` in a checkout). Expected output:

<!-- doctest: step1-expect -->
```text
small call: allowed
big call: refused budget_exhausted
```

The refused call never reached the provider. The cap is a lifetime budget of the ledger file
(`guide-ledger.db`); delete the file to start over. Details: [library mode](../library-mode.md).

**Protects against:** an honest script overspending: a runaway loop, a wrong model, a forgotten batch.

**Does not protect against:** anything that skips the guard. Library mode is **cooperative**: the
process holds the provider key, so any code in it can call the provider directly.

## Step 2: Local proxy

**What you get:** one gateway process on your machine. Apps use a spendgate key instead of the provider
key, and the provider keys stay in the gateway. Budgets and usage are shared by every app.

Run from the checkout (`uvx spendgate serve --config ...` or an installed `spendgate serve ...` work
the same; the proxy itself needs no SDK extra, only the demo script below does):

<!-- doctest: step2-env -->
```bash
uv sync --extra openai --extra anthropic
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...
[ -f ~/.spendgate-pepper ] || (umask 077; openssl rand -hex 32 > ~/.spendgate-pepper)   # made once
export SPENDGATE_KEY_PEPPER=$(cat ~/.spendgate-pepper)   # keep the file: changing it voids every key
```

Use the same exports in every terminal below (the pepper must be the same each time, or keys stop working). Check the config and create the ledger, then start the
gateway in its own terminal ([`examples/config.local.yaml`](../../examples/config.local.yaml): SQLite,
127.0.0.1:8080, a 5 USD cap on the scope `org:acme/project:app`):

<!-- doctest: step2-setup -->
```bash
uv run spendgate check-config --config examples/config.local.yaml
uv run spendgate migrate --bootstrap --config examples/config.local.yaml
```

<!-- doctest: step2-serve -->
```bash
uv run spendgate serve --config examples/config.local.yaml
```

Create a key. The secret is printed once:

<!-- doctest: step2-key -->
```bash
uv run spendgate keys create --local --config examples/config.local.yaml --scope org:acme/project:app --name my-app
```

```bash
export SPENDGATE_API_KEY=sg-...         # the secret printed above
```

Point the SDKs at the gateway. The OpenAI base URL ends in `/v1`; the Anthropic one does not. Save as
`proxy_demo.py`:

<!-- doctest: step2-python -->
```python
import os

import anthropic
import openai

key = os.environ["SPENDGATE_API_KEY"]

oa = openai.OpenAI(base_url="http://127.0.0.1:8080/v1", api_key=key)
reply = oa.chat.completions.create(
    model="gpt-6-luna", messages=[{"role": "user", "content": "hi"}], max_tokens=20
)
print("openai ok:", reply.model)

an = anthropic.Anthropic(base_url="http://127.0.0.1:8080", api_key=key)
reply = an.messages.create(
    model="claude-haiku", messages=[{"role": "user", "content": "hi"}], max_tokens=20
)
print("anthropic ok:", reply.model)
```

<!-- doctest: step2-run -->
```bash
uv run python proxy_demo.py
```

**Verify:** the calls show up in the ledger, by model:

<!-- doctest: step2-usage -->
```bash
uv run spendgate usage --local --config examples/config.local.yaml --by model
```

Expect one row each for `gpt-6-luna` and `claude-haiku` with a cost. Stop the gateway with Ctrl-C.

**Protects against:** overspending by anything that uses the gateway key: the cap, rate limits and kill
switches apply to every app, and each app can be revoked on its own. The apps never see a provider key.

**Does not protect against:** an app that also holds a real provider key (or can read your shell's
environment) and calls the provider directly. The gateway cannot see that traffic. For that, use step 3.

## Step 3: Docker Compose with the perimeter

**What you get:** the gateway in a container on an internal network. App containers can reach the guard
and nothing else, so they cannot call a provider even with a key. The provider keys exist only in the
guard's container.

Prerequisites: Docker with Compose v2.20+, provider keys.

<!-- doctest: step3-up -->
```bash
cd deploy/compose
umask 077 && mkdir -p secrets
printf '%s' "$OPENAI_API_KEY"    > secrets/openai_api_key
printf '%s' "$ANTHROPIC_API_KEY" > secrets/anthropic_api_key
openssl rand -hex 32 | tr -d '\n' > secrets/spendgate_key_pepper
: > secrets/spendgate_database_url; : > secrets/postgres_password
chmod 700 secrets && chmod 644 secrets/*
docker compose build
docker compose run --rm spendgate migrate
docker compose up -d
```

Migrating is its own step: `migrate` creates the ledger now and upgrades it after you install a new
release (see [Upgrades and rollbacks](../operations.md#upgrades-and-rollbacks)).

**Verify** the guard and the perimeter (the demo app prints both; exit code 0 means the perimeter holds):

<!-- doctest: step3-verify -->
```bash
docker compose run --rm --no-deps demo
# guard   http://spendgate:8080/readyz -> 200
# direct  api.openai.com does not resolve (...): the perimeter holds
# direct  1.1.1.1:443 unreachable (...): the perimeter holds
```

Create a key (same scope as step 2) and use it from an app container (point the OpenAI SDK at `http://spendgate:8080/v1`; the
Anthropic SDK at `http://spendgate:8080`):

<!-- doctest: step3-key -->
```bash
docker compose exec spendgate spendgate keys create --local --scope org:acme/project:app --name my-app
```

<!-- doctest: step3-use -->
```bash
docker compose run --rm -e SPENDGATE_API_KEY=sg-... demo      # expect "with SPENDGATE_API_KEY -> 200"
docker compose exec spendgate spendgate usage --local --by model
```

Your own app joins the `apps` network and nothing else; give it only the virtual key (`sg-...`), never a
provider key. To call from the host, uncomment the loopback `ports:` line in `compose.yaml`; that opens the
guard to every process on the host.

To use the released image instead of building one, verify it first (see
[Container image](#container-image)), then pull it by the digest `cosign verify` printed, tag it with
the name Compose expects, migrate, and start without building:

```bash
docker pull "ghcr.io/jmmfcoutinho/spendgate@$digest"     # $digest from the verify step
docker tag "ghcr.io/jmmfcoutinho/spendgate@$digest" spendgate:core
docker compose run --rm spendgate migrate
docker compose up -d
```

For the `full` variant (Postgres ledger, Bedrock, Google, Azure), verify `:<version>-full` and use its
own digest:

```bash
docker pull "ghcr.io/jmmfcoutinho/spendgate@$digest"     # the -full image's digest
docker tag "ghcr.io/jmmfcoutinho/spendgate@$digest" spendgate:full
SPENDGATE_VARIANT=full docker compose run --rm spendgate migrate
SPENDGATE_VARIANT=full docker compose up -d
```

Postgres, publishing the admin API, putting it on its own network for an operator container
(`compose.admin.yaml`) and the full guarantee list are in
[`deploy/compose/README.md`](../../deploy/compose/README.md). Stop and delete everything with
`docker compose down -v`.

**Protects against:** apps reaching models by any route except the guard. This is enforced by the network,
not by the apps' good behaviour: a leaked provider key is useless from the `apps` network.

**Does not protect against:** the guard itself. It must reach providers, so a compromised guard (or a
bug that sends a request to the wrong host) can reach any host. The
[threat model](../threat-model.md) lists this as an accepted risk; an egress host allowlist is on
the [roadmap](../../ROADMAP.md). Also: the Compose ledger is in EUR with its own sample budgets; edit
`deploy/compose/spendgate.sqlite.yaml` to change them.

## Container image

Each release publishes two images to GitHub Container Registry, built in CI from the tagged commit
with an SBOM and provenance, and signed keyless with [cosign](https://docs.sigstore.dev/):

- `ghcr.io/jmmfcoutinho/spendgate:<version>`: the base install (SQLite ledger, no optional extra).
- `ghcr.io/jmmfcoutinho/spendgate:<version>-full`: adds the `postgres`, `bedrock`, `google`,
  `azure` and `tokenizer` extras (the Postgres ledger and the credentials those providers need).

The image carries its own Python, so the host needs neither Python 3.13 nor a checkout. It runs as
a non-root user; the entrypoint is `spendgate` and the default command is
`serve --config /etc/spendgate/spendgate.yaml` (data port 8080, admin port 8081, a `/data`
volume for the SQLite ledger). Verify the signature before you run it; the identity is the
release workflow at the version's tag (the command below also needs `jq`):

```bash
v=0.1.1          # for the full image, verify "ghcr.io/jmmfcoutinho/spendgate:$v-full" instead
digest=$(cosign verify "ghcr.io/jmmfcoutinho/spendgate:$v" \
  --certificate-identity "https://github.com/jmmfcoutinho/spendgate/.github/workflows/release.yml@refs/tags/v$v" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  | jq -r '.[0].critical.image."docker-manifest-digest"')
echo "$digest"   # sha256:...: the signed image
```

A failed verification exits non-zero and prints no digest. Pull and run the image by that digest
(`ghcr.io/jmmfcoutinho/spendgate@$digest`), never by the tag alone, and pin the same digest in your
own deployment files: a tag can be moved, a digest cannot.
