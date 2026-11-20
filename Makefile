# Same commands as CONTRIBUTING.md section 3; CI calls these targets.
.PHONY: check lint format-check format type arch test test-postgres load fuzz secrets audit size

check: lint format-check type arch test secrets audit size

lint:
	uv run ruff check .

format-check:
	uv run ruff format --check .

format:
	uv run ruff format .

type:
	uv run mypy --strict src tests scripts

arch:
	uv run lint-imports

test:
	uv run pytest -m "not live and not load and not fuzz" --cov=spendgate.core --cov=spendgate.ledger \
		--cov=spendgate.pipeline --cov-fail-under=90

load:
	uv run pytest -m load tests/load -q

# Short budget by default (200 examples per case). SPENDGATE_FUZZ_SECONDS=600 gives each target
# (one test file) that many wall-clock seconds; run one file per process to parallelise.
# SPENDGATE_FUZZ_SEED fixes the seed, SPENDGATE_FUZZ_REPORT_DIR collects one JSON report per target.
fuzz:
	uv run pytest -m fuzz tests/fuzz -q -s

test-postgres:
	uv run pytest -m postgres -q

secrets:
	git ls-files -z | xargs -0 uv run detect-secrets-hook --baseline .secrets.baseline  # pragma: allowlist secret

# Audit the locked set, not whatever happens to be installed; the project itself is not on PyPI.
audit:
	uv export --frozen --format requirements-txt --no-emit-project 2>/dev/null \
		| uv run pip-audit -r /dev/stdin --no-deps --disable-pip --strict

size:
	uv run python scripts/lightweight_report.py
