# syntax=docker/dockerfile:1
# Both digests are multi-platform indexes verified against the upstream registries.
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv
FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS builder

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python3.13
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python3.13

FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS runtime
LABEL org.opencontainers.image.title="WAAGD Discovery" \
      org.opencontainers.image.source="https://github.com/mikemmagik/waagd-discovery" \
      org.opencontainers.image.description="One-shot RSS/Atom discovery with persistent SQLite storage"
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WAAGD_DB=/data/discovery.sqlite3 \
    WAAGD_SOURCES=/app/config/sources.yaml \
    WAAGD_FILTER=/app/config/filter.yaml \
    WAAGD_BACKUP_POLICY=/app/config/backup.yaml
RUN groupadd --gid 10001 discovery \
    && useradd --uid 10001 --gid discovery --no-create-home --shell /usr/sbin/nologin discovery \
    && mkdir -p /data /exports /backups /app/config \
    && chown discovery:discovery /data /exports /backups \
    && chmod 0700 /data /exports /backups
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY config/sources.yaml config/filter.yaml config/backup.yaml ./config/
USER 10001:10001
ENTRYPOINT ["waagd-discovery"]
CMD ["stats"]
