from __future__ import annotations

import pytest

from graylog_mcp.config import parse_config
from graylog_mcp.tools import App
from tests.fake_graylog import FakeGraylog

VERSIONS = ["4.3.15+1234567", "5.0.13+083613e", "5.2.4", "6.1.2", "7.0.1"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in [
        "GRAYLOG_URL",
        "GRAYLOG_TOKEN",
        "GRAYLOG_USERNAME",
        "GRAYLOG_PASSWORD",
        "GRAYLOG_MCP_CONFIG",
        "GRAYLOG_TIMEZONE",
        "GRAYLOG_REDACTION_PACKS",
        "GRAYLOG_APP_PACKAGES",
        "GRAYLOG_MCP_HTTP_TOKEN",
        "GRAYLOG_VERIFY_TLS",
        "GRAYLOG_CA_BUNDLE",
        "GRAYLOG_PROXY",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent-graylog-mcp-test")
    monkeypatch.setenv("TEST_GRAYLOG_TOKEN", "secret-token")


def make_config(**overrides):
    data = {
        "timezone": "Asia/Ho_Chi_Minh",
        "redaction": {"packs": ["vn"]},
        "stacktrace": {"app_packages": ["com.acme"]},
        "instances": {"main": {"url": "https://graylog.test", "token_env": "TEST_GRAYLOG_TOKEN"}},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return parse_config(data, source="test")


@pytest.fixture
def make_app():
    apps = []

    def factory(version: str = "5.0.13+083613e", dataset: str = "basic", **overrides):
        fake = FakeGraylog(version, dataset=dataset)
        app = App.create(make_config(**overrides), transport=fake.transport)
        apps.append(app)
        return app, fake

    yield factory


@pytest.fixture(params=VERSIONS)
def version(request):
    return request.param
