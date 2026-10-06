import pytest

from graylog_mcp.config import ConfigError, load_config, parse_config


def test_env_only(monkeypatch):
    monkeypatch.setenv("GRAYLOG_URL", "https://gl.example.com/api/")
    monkeypatch.setenv("GRAYLOG_TOKEN", "tok")
    cfg = parse_config({})
    inst = cfg.instance(None)
    assert inst.url == "https://gl.example.com"
    assert inst.api_base == "https://gl.example.com/api/"
    assert inst.token == "tok" and inst.auth == "token"
    assert cfg.default_instance == "default"


def test_env_basic_auth(monkeypatch):
    monkeypatch.setenv("GRAYLOG_URL", "http://localhost:9000")
    monkeypatch.setenv("GRAYLOG_USERNAME", "admin")
    monkeypatch.setenv("GRAYLOG_PASSWORD", "pw")
    monkeypatch.setenv("GRAYLOG_VERIFY_TLS", "false")
    inst = parse_config({}).instance(None)
    assert inst.auth == "basic" and inst.username == "admin" and inst.password == "pw"
    assert inst.verify_tls is False


def test_nothing_configured():
    with pytest.raises(ConfigError, match="GRAYLOG_URL"):
        parse_config({})


def test_missing_token_env(monkeypatch):
    monkeypatch.setenv("GRAYLOG_URL", "https://gl.example.com")
    with pytest.raises(ConfigError, match="GRAYLOG_TOKEN"):
        parse_config({})


def test_full_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PROD_TOKEN", "p")
    monkeypatch.setenv("STAGING_PW", "s")
    path = tmp_path / "c.toml"
    path.write_text(
        """
default_instance = "prod"
timezone = "Asia/Ho_Chi_Minh"

[limits]
default_limit = 20
max_output_chars = 12000

[redaction]
packs = ["vn", "eu"]
vn_cmnd = true
exclude_fields = ["token_count"]
allow = ['^noreply@acme\\.vn$']

[[redaction.patterns]]
name = "order"
pattern = "ORD-[0-9]{6}"

[stacktrace]
app_packages = ["vn.acme"]

[investigation]
trace_fields = ["traceId", "X-Request-ID"]
error_query = "level:<=3 OR level:ERROR"
group_fields = { exception = "ExceptionType" }

[instances.prod]
url = "https://graylog.acme.vn"
token_env = "PROD_TOKEN"
timezone = "UTC"
proxy = "http://proxy:3128"

[instances.staging]
url = "https://graylog-stg.acme.vn"
auth = "basic"
username = "readonly"
password_env = "STAGING_PW"
verify_tls = false
error_query = "level:3"
aggregation_api = "views"

[presets.slow]
description = "slow requests"
tool = "search_logs"
args = { query = "took_ms:>1000", range = "1h" }
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    prod, stg = cfg.instance("prod"), cfg.instance("staging")
    assert cfg.default_instance == "prod"
    assert prod.timezone == "UTC" and stg.timezone == "Asia/Ho_Chi_Minh"
    assert prod.trace_fields == ("traceId", "X-Request-ID")
    assert prod.error_query == "level:<=3 OR level:ERROR" and stg.error_query == "level:3"
    assert prod.group_fields["exception"] == "ExceptionType" and prod.group_fields["source"] == "source"
    assert stg.auth == "basic" and stg.password == "s" and stg.aggregation_api == "views"
    assert cfg.limits.default_limit == 20
    assert cfg.redaction.packs == ("vn", "eu") and cfg.redaction.patterns[0].replacement == "[ORDER]"
    assert cfg.presets["slow"].args["query"] == "took_ms:>1000"
    with pytest.raises(ConfigError, match="unknown instance"):
        cfg.instance("nope")


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"instances": {"a": {"url": "https://x", "token": "plain"}}}, "must not be written"),
        ({"instances": {"a": {"url": "ftp://x", "token_env": "T"}}}, "url must look like"),
        ({"instances": {"a": {"url": "https://u:p@x", "token_env": "T"}}}, "credentials"),
        ({"instances": {"a": {"url": "https://x", "token_env": "T", "colour": 1}}}, "unknown key"),
        ({"timezone": "Mars/Base", "instances": {"a": {"url": "https://x", "token_env": "T"}}}, "timezone"),
        ({"redaction": {"packs": ["xx"]}, "instances": {"a": {"url": "https://x", "token_env": "T"}}}, "pack"),
        (
            {"redaction": {"patterns": [{"pattern": "("}]}, "instances": {"a": {"url": "https://x", "token_env": "T"}}},
            "invalid regex",
        ),
        ({"limits": {"default_limit": 0}, "instances": {"a": {"url": "https://x", "token_env": "T"}}}, "positive"),
        ({"presets": {"p": {"tool": "rm_rf"}}, "instances": {"a": {"url": "https://x", "token_env": "T"}}}, "tool"),
        ({"default_instance": "b", "instances": {"a": {"url": "https://x", "token_env": "T"}}}, "default_instance"),
        ({"instances": {"a": {"url": "https://x", "token_env": "T", "message_api": "x"}}}, "message_api"),
    ],
)
def test_validation_errors(monkeypatch, data, match):
    monkeypatch.setenv("T", "t")
    with pytest.raises(ConfigError, match=match):
        parse_config(data)


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/graylog-mcp.toml")


def test_project_config_discovery(tmp_path, monkeypatch):
    repo = tmp_path / "app"
    (repo / ".git").mkdir(parents=True)
    (repo / "services" / "api").mkdir(parents=True)
    (repo / ".graylog-mcp.toml").write_text(
        """
default_instance = "staging"
[instances.staging]
url = "https://graylog-stg.example.com"
token_env = "STG_TOKEN"
description = "Staging, deployed on every merge"
[instances.prod]
url = "https://graylog.example.com"
token_env = "PROD_TOKEN"
description = "Production"
""",
        encoding="utf-8",
    )
    (tmp_path / ".graylog-mcp.toml").write_text("this is outside the repo and must be ignored", encoding="utf-8")
    monkeypatch.setenv("STG_TOKEN", "s")
    monkeypatch.setenv("PROD_TOKEN", "p")
    monkeypatch.chdir(repo / "services" / "api")
    cfg = load_config()
    assert cfg.source.endswith(".graylog-mcp.toml") and cfg.default_instance == "staging"
    assert cfg.instance("prod").description == "Production"
    assert cfg.instance("staging").token == "s"


def test_project_config_not_searched_beyond_repo(tmp_path, monkeypatch):
    from graylog_mcp.config import find_project_config

    (tmp_path / ".graylog-mcp.toml").write_text("", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert find_project_config(repo) is None
