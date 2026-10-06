"""Groups (ERP, CXP, PAYMENT...) x user-defined environments, includes and instance resolution."""

from __future__ import annotations

import pytest

from graylog_mcp import tools
from graylog_mcp.config import ConfigError, load_config, parse_config
from graylog_mcp.tools import App
from tests.fake_graylog import FakeGraylog

ORG = {
    "default_environment": "uat",
    "environments": {
        "uat": {"description": "User acceptance", "timeout": 60},
        "prod": {"description": "Production", "error_query": "level:<=2"},
        "sandbox": {"description": "Partner sandbox"},
    },
    "groups": {
        "erp": {
            "description": "ERP",
            "trace_fields": ["correlationId"],
            "environments": {
                "uat": {"url": "https://gl-erp-uat.test", "token_env": "ERP_UAT_T"},
                "prod": {"url": "https://gl-erp.test", "token_env": "ERP_PROD_T", "error_query": "level:<=3"},
            },
        },
        "payment": {
            "description": "Payment platform",
            "default_environment": "sandbox",
            "ca_bundle_note": None,
            "environments": {
                "sandbox": {"url": "https://gl-pay-sbx.test", "token_env": "PAY_SBX_T"},
                "prod": {"url": "https://gl-pay.test", "token_env": "PAY_PROD_T"},
            },
        },
        "cxp": {"environments": {"prod": {"url": "https://gl-cxp.test", "token_env": "CXP_PROD_T"}}},
    },
}


@pytest.fixture
def org(monkeypatch):
    for name in ("ERP_UAT_T", "ERP_PROD_T", "PAY_SBX_T", "PAY_PROD_T", "CXP_PROD_T"):
        monkeypatch.setenv(name, "t")
    data = {k: v for k, v in ORG.items()}
    data["groups"] = {g: {k: v for k, v in d.items() if k != "ca_bundle_note"} for g, d in ORG["groups"].items()}
    return parse_config(data)


def test_layering(org):
    erp_uat, erp_prod = org.instances["erp/uat"], org.instances["erp/prod"]
    assert (erp_uat.group, erp_uat.environment) == ("erp", "uat")
    assert erp_uat.timeout == 60  # from [environments.uat]
    assert erp_uat.trace_fields == ("correlationId",)  # from [groups.erp]
    assert erp_prod.error_query == "level:<=3"  # group environment beats [environments.prod]
    assert org.instances["payment/prod"].error_query == "level:<=2"  # inherited from [environments.prod]
    assert erp_uat.description == "ERP (User acceptance)"
    assert org.instances["cxp/prod"].description == "cxp Production"
    assert set(org.environments) == {"uat", "prod", "sandbox"}


def test_resolution(org):
    assert org.instance("payment/prod").name == "payment/prod"
    assert org.instance("PAYMENT prod").name == "payment/prod"
    assert org.instance("erp:uat").name == "erp/uat"
    assert org.instance("payment").name == "payment/sandbox"  # group default environment
    assert org.instance("erp").name == "erp/uat"  # global default_environment
    assert org.instance("sandbox").name == "payment/sandbox"  # only one group has it
    with pytest.raises(ConfigError, match="several groups"):
        org.instance("prod")
    two_envs = {
        "a": {"url": "https://a", "token_env": "ERP_UAT_T"},
        "b": {"url": "https://b", "token_env": "ERP_UAT_T"},
    }
    with pytest.raises(ConfigError, match="several environments"):
        parse_config({"groups": {"x": {"environments": two_envs}}}).instance("x")
    assert org.instance("cxp").name == "cxp/prod"  # single environment
    with pytest.raises(ConfigError, match="Did you mean"):
        org.instance("paymnt/prod")


def test_default_group(monkeypatch, org):
    data = {**ORG, "default_group": "payment"}
    data["groups"] = {g: {k: v for k, v in d.items() if k != "ca_bundle_note"} for g, d in ORG["groups"].items()}
    cfg = parse_config(data)
    assert cfg.default_instance == "payment/sandbox"
    assert cfg.instance("prod").name == "payment/prod"  # default group wins the ambiguity
    assert cfg.instance(None).name == "payment/sandbox"


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"groups": {"g": {}}}, "at least one"),
        (
            {
                "groups": {
                    "g": {"environments": {"p": {"url": "https://x", "token_env": "T"}}, "default_environment": "q"}
                }
            },
            "default_environment",
        ),
        (
            {"groups": {"g": {"colour": 1, "environments": {"p": {"url": "https://x", "token_env": "T"}}}}},
            "unknown key",
        ),
        ({"groups": {"g": {"token_env": "T", "environments": {"p": {"url": "https://x"}}}}}, "unknown key"),
        ({"environments": {"prod": {"url": "https://x"}}, "groups": {}}, "unknown key"),
        (
            {"default_group": "nope", "groups": {"g": {"environments": {"p": {"url": "https://x", "token_env": "T"}}}}},
            "default_group",
        ),
        (
            {
                "instances": {"g/p": {"url": "https://x", "token_env": "T"}},
                "groups": {"g": {"environments": {"p": {"url": "https://y", "token_env": "T"}}}},
            },
            "both",
        ),
        (
            {"include": "x.toml", "instances": {"a": {"url": "https://x", "token_env": "T"}}},
            "only works in a config file",
        ),
    ],
)
def test_validation(monkeypatch, data, match):
    monkeypatch.setenv("T", "t")
    with pytest.raises(ConfigError, match=match):
        parse_config(data)


def test_flat_instances_with_labels_get_environment_defaults(monkeypatch):
    monkeypatch.setenv("T", "t")
    cfg = parse_config(
        {
            "environments": {"prod": {"timeout": 90}},
            "instances": {"erp-prod": {"url": "https://x", "token_env": "T", "group": "erp", "environment": "prod"}},
        }
    )
    inst = cfg.instance("erp/prod")
    assert inst.name == "erp-prod" and inst.timeout == 90 and "erp" in cfg.groups


def test_include_org_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PAY_SBX_T", "t")
    monkeypatch.setenv("PAY_PROD_T", "t")
    shared = tmp_path / "platform" / "graylog-org.toml"
    shared.parent.mkdir()
    shared.write_text(
        """
timezone = "Asia/Ho_Chi_Minh"
[environments.prod]
description = "Production"
[groups.payment]
description = "Payment"
[groups.payment.environments.sandbox]
url = "https://gl-pay-sbx.test"
token_env = "PAY_SBX_T"
[groups.payment.environments.prod]
url = "https://gl-pay.test"
token_env = "PAY_PROD_T"
""",
        encoding="utf-8",
    )
    repo = tmp_path / "payment-api"
    repo.mkdir()
    project = repo / ".graylog-mcp.toml"
    project.write_text(
        'include = "../platform/graylog-org.toml"\ndefault_group = "payment"\ndefault_environment = "sandbox"\n'
        '[groups.payment]\nservice_fields = ["app"]\n',
        encoding="utf-8",
    )
    cfg = load_config(project)
    assert cfg.default_instance == "payment/sandbox"
    assert cfg.instances["payment/prod"].service_fields == ("app",)  # project override merged into the group
    assert cfg.instances["payment/prod"].timezone == "Asia/Ho_Chi_Minh"

    shared.write_text('include = "../payment-api/.graylog-mcp.toml"\n' + shared.read_text(), encoding="utf-8")
    with pytest.raises(ConfigError, match="include cycle"):
        load_config(project)


async def test_tools_and_list_instances_by_group(org, monkeypatch):
    fake = FakeGraylog("6.1.2")
    app = App.create(org, transport=fake.transport)
    listing = await tools.list_instances(app)
    groups = {g["group"]: g for g in listing["groups"]}
    assert groups["payment"]["environments"] == ["prod", "sandbox"]
    assert groups["payment"]["default_environment"] == "sandbox"
    assert listing["environments"]["uat"] == "User acceptance"
    status = {i["name"]: i for i in listing["instances"]}
    assert status["erp/prod"]["group"] == "erp" and status["erp/prod"]["environment"] == "prod"
    res = await tools.count_logs(app, range="2h", instance="payment prod")
    assert res["instance"] == "payment/prod" and res["count"] > 0
