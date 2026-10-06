# Streamable HTTP image for running one shared server for a team.
#   docker run -p 8000:8000 -e GRAYLOG_URL=... -e GRAYLOG_TOKEN=... -e GRAYLOG_MCP_HTTP_TOKEN=... graylog-mcp
FROM python:3.13-slim AS build
RUN pip install --no-cache-dir "uv>=0.8,<1"
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# dependency versions come from uv.lock
RUN uv export --locked --no-dev --no-emit-project --no-hashes -o requirements.txt \
    && uv build --wheel --out-dir /dist

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /src/requirements.txt /dist/*.whl /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir --no-deps /tmp/*.whl \
    && rm -f /tmp/*.whl /tmp/requirements.txt \
    && useradd --system --uid 10001 --no-create-home mcp
USER mcp
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
ENTRYPOINT ["graylog-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
