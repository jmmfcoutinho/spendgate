# Same commands as CONTRIBUTING.md section 3; CI calls these targets.
.PHONY: check lint format-check format type arch test coverage-gates test-postgres load fuzz \
	secrets audit size

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

# Coverage (CONTRIBUTING.md section 2): `make test` measures all of src/spendgate (line and branch)
# and fails below 90% overall, then `coverage-gates` holds each money package to its own floor, so
# well-covered code elsewhere cannot hide a gap in one of them. Without a Postgres server the
# ledger floor counts the Postgres backend at what the SQLite run reaches; `make test-postgres`
# gates that backend on its own run (CI runs both).
COVERAGE_GATES := core:95 ledger:90 pipeline:90
COVERAGE_POSTGRES_FLOOR := 90
# Not `.coverage.*`: pytest-cov combines those into `.coverage`, which would mix the two runs.
COVERAGE_POSTGRES_FILE := .coverage-postgres

test:
	uv run pytest -m "not live and not load and not fuzz" --cov --cov-fail-under=90
	@$(MAKE) --no-print-directory coverage-gates

# Reads the `.coverage` file the last `make test` wrote.
coverage-gates:
	@set -e; for gate in $(COVERAGE_GATES); do \
		pkg=$${gate%:*}; floor=$${gate#*:}; \
		echo "coverage gate: spendgate.$$pkg >= $$floor%"; \
		uv run coverage report --include="src/spendgate/$$pkg/*" --format=total \
			--fail-under=$$floor; \
	done

load:
	uv run pytest -m load tests/load -q

# Short budget by default (200 examples per case). SPENDGATE_FUZZ_SECONDS=600 gives each target
# (one test file) that many wall-clock seconds; run one file per process to parallelise.
# SPENDGATE_FUZZ_SEED fixes the seed, SPENDGATE_FUZZ_REPORT_DIR collects one JSON report per target.
fuzz:
	uv run pytest -m fuzz tests/fuzz -q -s

# Needs SPENDGATE_TEST_POSTGRES_URL: without it every test skips and the gate would fail on an
# empty run. Its coverage file is separate from the one `make test` writes.
test-postgres:
	@test -n "$$SPENDGATE_TEST_POSTGRES_URL" || { echo "set SPENDGATE_TEST_POSTGRES_URL" >&2; exit 2; }
	COVERAGE_FILE=$(COVERAGE_POSTGRES_FILE) uv run pytest -m postgres -q --cov --cov-report=
	@echo "coverage gate: spendgate.ledger.postgres* >= $(COVERAGE_POSTGRES_FLOOR)% (Postgres run)"
	uv run coverage report --data-file=$(COVERAGE_POSTGRES_FILE) \
		--include="src/spendgate/ledger/postgres*" --fail-under=$(COVERAGE_POSTGRES_FLOOR)

secrets:
	git ls-files -z | xargs -0 uv run detect-secrets-hook --baseline .secrets.baseline  # pragma: allowlist secret

# Audit the locked set, not whatever happens to be installed; the project itself is not on PyPI.
audit:
	uv export --frozen --format requirements-txt --no-emit-project 2>/dev/null \
		| uv run pip-audit -r /dev/stdin --no-deps --disable-pip --strict

size:
	uv run python scripts/lightweight_report.py
