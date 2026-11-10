# Streamable HTTP image for running one shared server for a team.
#   docker run -p 8000:8000 -e GRAYLOG_URL=... -e GRAYLOG_TOKEN=... -e GRAYLOG_MCP_HTTP_TOKEN=... graylog-mcp
# Base images are pinned by digest; Dependabot updates the tag and the digest together.
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# dependency versions come from uv.lock
RUN uv export --locked --no-dev --no-emit-project --no-hashes -o requirements.txt \
    && uv build --wheel --out-dir /dist

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /src/requirements.txt /dist/*.whl /tmp/
# Debian security fixes land before the next base image does; CI scans the result
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir --no-deps /tmp/*.whl \
    && rm -f /tmp/*.whl /tmp/requirements.txt \
    && useradd --system --uid 10001 --no-create-home mcp
USER mcp
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
ENTRYPOINT ["graylog-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
