# syntax=docker/dockerfile:1
# Hardened, multi-stage image with two variants. All bases pinned by digest; the tag is recorded
# beside each one.
#   docker build --target core -t spendgate:core .   base install (SQLite, OpenAI, Anthropic)
#   docker build --target full -t spendgate:full .   core + postgres, bedrock, google, azure, tokenizer
# A plain `docker build .` builds the last stage, `core`. The `openai` and `anthropic` SDK extras are
# client-side and never installed here.

# uv:0.13.8 (the binary only; Python comes from the base below)
FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

# python:3.13-slim
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
COPY --from=uv /uv /usr/local/bin/uv
# A venv is not relocatable, so it is built at the path it runs from.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

FROM build AS build-core
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM build AS build-full
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
    --extra postgres --extra bedrock --extra google --extra azure --extra tokenizer

# python:3.13-slim
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS stripped
# Patch, then strip what a running service does not need. pip, the package managers, perl and
# bash go (pip's vendored libraries carry HIGH CVEs; there is nothing to install at runtime), and so
# do the stdlib test suite, IDLE and Tk. /var/lib/dpkg/status stays so scanners see the OS packages.
# zlib1g, libzstd1 (CVE-2026-124851) and glibc's libc6 and libc-bin (CVE-2026-18374, -19499, -19542,
# -5435) are upgraded to the Debian security fixes the pinned base digest predates; drop each once
# Dependabot refreshes the digest past it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade zlib1g libzstd1 libc6 libc-bin \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt /var/cache/debconf /var/log/apt /var/log/dpkg.log \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip* /usr/local/bin/pip* \
       /usr/local/lib/python3.13/ensurepip /usr/local/lib/python3.13/idlelib \
       /usr/local/lib/python3.13/tkinter /usr/local/lib/python3.13/turtledemo \
       /usr/local/lib/python3.13/test /usr/local/lib/python3.13/lib-dynload/_tkinter* \
       /usr/local/bin/idle* /usr/local/bin/pydoc* /usr/local/include /usr/local/lib/python3.13/pydoc_data \
       /usr/lib/*-linux-gnu/gconv /usr/lib/*-linux-gnu/perl-base /usr/lib/*-linux-gnu/libapt-* \
       /usr/lib/apt /usr/share/doc /usr/share/man /usr/share/info /usr/share/perl* \
    && rm -f /usr/bin/apt* /usr/bin/dpkg* /usr/bin/perl* /usr/bin/sqv /usr/bin/openssl \
       /usr/bin/bash /bin/bash /usr/bin/rbash \
    && groupadd --system --gid 10001 spendgate \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin spendgate \
    && mkdir /data && chown 10001:10001 /data && chmod 0700 /data

# Flatten: deleted files in the layers above would otherwise still count towards the image size.
FROM scratch AS runtime
COPY --from=stripped / /
LABEL org.opencontainers.image.title="spendgate" \
      org.opencontainers.image.description="Small, auditable LLM spend guard (SDK and proxy)" \
      org.opencontainers.image.source="https://github.com/jmmfcoutinho/spendgate" \
      org.opencontainers.image.licenses="Apache-2.0"
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# Stdlib only: no curl in the image. 8080 is the data port of the reference config.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/livez', timeout=3).read()"]
VOLUME ["/data"]
# 8080 data plane, 8081 admin API (bind it in the config; never publish it beyond localhost).
EXPOSE 8080 8081
USER 10001:10001
ENTRYPOINT ["spendgate"]
CMD ["serve", "--config", "/etc/spendgate/spendgate.yaml"]

FROM runtime AS full
COPY --from=build-full /app/.venv /app/.venv

FROM runtime AS core
COPY --from=build-core /app/.venv /app/.venv
