import re
import time

import pytest

from graylog_mcp.config import CustomPattern, RedactionConfig, StacktraceConfig
from graylog_mcp.redact import Redactor, luhn_ok
from graylog_mcp.shaping import compact_stacktrace

CORE = Redactor()
ALL = Redactor(RedactionConfig(packs=("vn", "us", "eu", "uk", "in"), vn_cmnd=True))


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("login a.b+c@example.co.uk failed", "a.b+c@example.co.uk"),
        ("password=hunter2&user=x", "hunter2"),
        ("db_password: hunter2", "hunter2"),
        ('{"password": "s3cr3t!", "user": "x"}', "s3cr3t!"),
        ("{'api_key': 'AKIA123456'}", "AKIA123456"),
        ('client_secret="abc def"', "abc def"),
        ("accessToken=xyz123", "xyz123"),
        ("Authorization: Bearer abcdefgh12345678", "abcdefgh12345678"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("jdbc:postgresql://admin:pw123@db:5432/x", "pw123"),
        ("redis://:supersecret@cache:6379", "supersecret"),
        (
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        ),
        ("card 4111 1111 1111 1111 ok", "4111 1111 1111 1111"),
        ("card 4111-1111-1111-1111 ok", "4111-1111-1111-1111"),
        ("amex 378282246310005", "378282246310005"),
        ("mc 5555555555554444", "5555555555554444"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
        ("x-api-key: k-123456", "k-123456"),
    ],
)
def test_core_masks(text, secret):
    out = CORE.text(text)
    assert secret not in out, out


@pytest.mark.parametrize(
    "text",
    [
        "token validation failed for request",
        "bypass=true compass: north",
        "author=John Doe",
        "GET /api/orders/12345 200 in 15ms",
        "timestamp 20240501123045 epoch 1714550400123",
        "order 1234567890123 shipped",  # 13 digits, fails Luhn / prefix
        "uuid 550e8400-e29b-41d4-a716-446655440000",
        "com.acme.Foo@6d06d69c",
        "version 6.1.2 build 1700000000",
        "took 3000ms, retries=3",
        "ip 10.0.0.1:8080",
    ],
)
def test_core_does_not_mask(text):
    assert CORE.text(text) == text


@pytest.mark.parametrize(
    ("text", "secret", "label"),
    [
        ("call 0912345678 now", "0912345678", "[PHONE]"),
        ("call +84 912 345 678 now", "912 345 678", "[PHONE]"),
        ("call 091.234.5678 now", "091.234.5678", "[PHONE]"),
        ("landline 02438123456", "02438123456", "[PHONE]"),
        ("cccd 001099012345", "001099012345", "[VN_ID]"),
        ("cmnd 123456789", "123456789", "[VN_ID]"),
        ("ssn 123-45-6789", "123-45-6789", "[SSN]"),
        ("iban DE89 3704 0044 0532 0130 00", "3704 0044 0532", "[IBAN]"),
        ("iban GB82WEST12345698765432", "GB82WEST12345698765432", "[IBAN]"),
        ("nino AB 12 34 56 C", "AB 12 34 56 C", "[NINO]"),
    ],
)
def test_packs(text, secret, label):
    out = ALL.text(text)
    assert secret not in out and label in out, out


def test_packs_off_by_default():
    assert CORE.text("call 0912345678") == "call 0912345678"


def test_vn_does_not_mask_non_phone_numbers():
    vn = Redactor(RedactionConfig(packs=("vn",)))
    for text in ["order 1234567890", "took 1700000000ms", "http 200 in 0.5s", "ip 192.168.10.20"]:
        assert vn.text(text) == text
    assert vn.text("cmnd 123456789") == "cmnd 123456789"  # optional, off


def test_invalid_iban_not_masked():
    assert ALL.text("code DE00 0000 0000 0000 0000 00X").startswith("code DE00")


def test_field_names():
    out = CORE.fields(
        {
            "password": "x",
            "userPassword": "y",
            "Authorization": "Bearer abc",
            "set-cookie": "a=b",
            "session_id": "s",
            "author": "z",
            "passenger": "p",
            "message": "ok",
        }
    )
    assert out["password"] == out["userPassword"] == out["Authorization"] == "[REDACTED]"
    assert out["set-cookie"] == out["session_id"] == "[REDACTED]"
    assert out["author"] == "z" and out["passenger"] == "p" and out["message"] == "ok"


def test_exclusions_and_custom_patterns():
    r = Redactor(
        RedactionConfig(
            exclude_fields=("token_count",),
            extra_sensitive_fields=("national_id",),
            allow=(re.compile(r"noreply@example\.com"),),
            patterns=(CustomPattern("order", re.compile(r"ORD-[A-Z0-9]{8}"), "[ORDER]"),),
        )
    )
    out = r.fields({"token_count": 12, "national_id": "123", "message": "from noreply@example.com, ORD-AB12CD34"})
    assert out["token_count"] == 12
    assert out["national_id"] == "[REDACTED]"
    assert out["message"] == "from noreply@example.com, [ORDER]"


def test_nested_values():
    out = CORE.value({"headers": {"cookie": "abc"}, "list": ["a@b.io"]})
    assert out == {"headers": {"cookie": "[REDACTED]"}, "list": ["[EMAIL]"]}


def test_luhn():
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")


# --------------------------------------------------------------------------- profiles


def _from_toml(**redaction):
    from graylog_mcp.config import _parse_redaction

    return Redactor(_parse_redaction(redaction))


def test_profile_basic_is_the_default():
    assert _from_toml().rules == Redactor().rules
    assert _from_toml(profile="basic").is_sensitive_field("full_name") is False


def test_profile_pii_masks_personal_fields_but_not_look_alikes():
    red = _from_toml(profile="pii")
    assert red.fields({"full_name": "An Nguyen", "phone": "x"}) == {"full_name": "[REDACTED]", "phone": "[REDACTED]"}
    # logger_name / ip_address / hostname must stay readable
    assert red.fields({"logger_name": "acme.Pay", "ip_address": "10.0.0.1"})["logger_name"] == "acme.Pay"


def test_profile_finance_adds_iban_pack():
    iban = "DE89 3704 0044 0532 0130 00"
    assert "[IBAN]" in _from_toml(profile="finance").text(f"pay {iban}")
    assert iban in _from_toml().text(f"pay {iban}")


def test_profile_strict_masks_ipv4_and_every_pack():
    red = _from_toml(profile="strict")
    assert red.text("from 203.0.113.7 ok") == "from [IP] ok"
    assert {"vn_cccd", "us_ssn", "iban", "uk_nino", "in_pan"} <= set(red.active_rules)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("refused to 203.0.113.7. retry", "refused to [IP]. retry"),
        ("refused to 203.0.113.7.", "refused to [IP]."),
        ("host.203.0.113.7:80", "host.[IP]:80"),
        ("build 1.2.3.4.5", "build 1.2.3.4.5"),  # five parts is not an address
    ],
)
def test_profile_strict_ipv4_edges(text, expected):
    assert _from_toml(profile="strict").text(text) == expected


@pytest.mark.parametrize(
    ("profile", "names"),
    [
        ("pii", ["firstName", "first-name", "first.name", "FIRST_NAME", "dateOfBirth", "customer.name", "homeAddress"]),
        ("finance", ["accountNumber", "bank-account", "taxId", "routingNumber"]),
        ("strict", ["firstName", "taxId"]),
    ],
)
def test_profile_fields_match_any_spelling(profile, names):
    red = _from_toml(profile=profile)
    assert red.fields(dict.fromkeys(names, "x")) == dict.fromkeys(names, "[REDACTED]")


def test_sensitive_fields_from_the_file_match_any_spelling():
    assert _from_toml(sensitive_fields=["order_note"]).is_sensitive_field("orderNote")
    assert not _from_toml(sensitive_fields=["_"]).is_sensitive_field("anything")


@pytest.mark.parametrize(
    "unit", ["a-", "a.", "a_", "ab-", "a+", "a%", "a:", "a=", "a/", "a@", "1.", "'a", 'a"', "x://"]
)
def test_long_values_cost_linear_time(unit):
    # a-b-c-... took 26 s at 32 KB when these regexes backtracked; this budget is far above linear
    started = time.monotonic()
    _from_toml(profile="strict").text(unit * (128_000 // len(unit)))
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "unit",
    [
        "eyJ-",
        "aeyJ",
        "eyJabcde.",
        "x-eyJ_",
        "(",
        "password=",
        "password=<a ",
        "password=[a ",
        "password:=",
        'password=\\"',
        "token:",
        ".eyJ",
        "4111 ",
        "1 ",
        "1-",
        "12 ",
        "1 1-",
        "://a:b",
        "a://",
        "secret ",
    ],
)
def test_more_long_values_cost_linear_time(unit):
    started = time.monotonic()
    _from_toml(profile="strict").text(unit * (128_000 // len(unit)))
    assert time.monotonic() - started < 5


def test_a_long_line_of_brackets_is_quick_to_shape():
    started = time.monotonic()
    compact_stacktrace("(" * 400_000 + "\nx", StacktraceConfig())
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "text",
    [
        "a.b.c.d.e.f.g.h.i.j.k.password=fake-value",
        "a_b_c_d_e_f_g_h_i_j_k_token=fake-value",
        '"spring.kafka.properties.sasl.jaas.config.ssl.key.store.password":"fake-value"',
        '"a-b-c-d-e-f-g-h-i-token": "fake-value"',
        "--password=fake-value",
        ".password=fake-value",
        "a..password=fake-value",
    ],
)
def test_secret_behind_many_prefixes_is_still_masked(text):
    assert "fake-value" not in Redactor().text(text)


@pytest.mark.parametrize(
    "text",
    [
        "postgres://admin:fake-value@db:5432",
        "a" * 70 + "://user:fake-value@localhost/x",
        "reqid=" + "ab12" * 20 + "postgres://admin:fake-value@10.0.0.1:5432",
    ],
)
def test_url_passwords_are_masked_whatever_comes_before(text):
    masked = Redactor().text(text)
    assert "fake-value" not in masked and "[REDACTED]@" in masked


@pytest.mark.parametrize(
    "text",
    [
        '{\\"password\\":\\"fake-value\\"}',
        ':password=>"fake-value"',
        "password => fake-value",
        'password="fake-value',
        "password=<fake-value>",
        "password=[fake-value]",
        "password=[fakevalue]",
        'password := "fake-value"',
        'password == "fake-value"',
        "password=<my fake-value>",
        "password=[my fake-value]",
        "userPassword=fake-value",
        "dbpassword=fake-value",
        "PGPASSWORD=fake-value",
        "secretKey=fake-value",
        "JSESSIONID=fake-value",
    ],
)
def test_secret_shapes_in_log_text(text):
    masked = Redactor().text(text)
    assert "fake-value" not in masked and "fakevalue" not in masked


@pytest.mark.parametrize(
    "text",
    ["bypass=1", "compass=north", "secretary=bob", "max_tokens=100", "password=[REDACTED]", "authority=root"],
)
def test_look_alikes_in_log_text_stay(text):
    assert Redactor().text(text) == text


@pytest.mark.parametrize(
    "name",
    [
        "accesstoken",
        "refreshtoken",
        "userpassword",
        "dbpassword",
        "db_password1",
        "dbPassword1",
        "accessTokenValue",
        "api_keys",
        "id_tokens",
        "req_cookies",
        "xapikey",
        "otpCode",
        "mysecret",
    ],
)
def test_core_field_names_match_common_spellings(name):
    assert Redactor().is_sensitive_field(name)


@pytest.mark.parametrize("name", ["total_tokens", "undersecretary", "logger_name", "hostname", "monkey"])
def test_core_field_names_leave_look_alikes(name):
    assert not Redactor().is_sensitive_field(name)


@pytest.mark.parametrize(
    "text",
    [
        "qty 1 4111 1111 1111 1111",
        "4111 1111 1111 1111 12/29",
        "5 4111111111111111",
        "4111.1111.1111.1111",
        "order 1234 4111 1111 1111 1111",
        "id=12345 4111 1111 1111 1111 end",
    ],
)
def test_a_card_next_to_another_number_is_masked(text):
    assert "[CARD]" in Redactor().text(text) and "1111 1111" not in Redactor().text(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("411 111 111 111 1111 1234", "[CARD] 1234"),
        ("41 11 11 11 11 11 11 11 1234", "[CARD] 1234"),
        ("card 37 8282 2463 10005 12345", "card [CARD] 12345"),
    ],
)
def test_a_card_in_any_grouping_is_masked_as_before(text, expected):
    assert Redactor().text(text) == expected


@pytest.mark.parametrize(
    "text", ["AT81 4982 9656 8389 9957", "1234 PT73 8600 3655 2491 6441 2096 6 5678", "DE89 3704 0044 0532 0130 00"]
)
def test_the_card_rules_leave_a_spaced_iban_to_its_own_rule(text):
    masked = _from_toml(packs=["eu"]).text(text)
    assert "[IBAN]" in masked and "[CARD]" not in masked


def test_every_card_in_a_list_is_masked():
    masked = Redactor().text("4111 1111 1111 1111 5500 0000 0000 0004")
    assert masked == "[CARD] [CARD]"


@pytest.mark.parametrize(
    "text",
    ["3.141592653589793", "12345678901234567", "1234.5678", "4111.1111", "4 5 6 7 8 9 1 2 3 4 5 6 7 8 9 1 2 3 4 5 6"],
)
def test_numbers_that_are_not_cards_stay(text):
    assert Redactor().text(text) == text


def test_a_token_inside_a_dashed_run_is_still_masked():
    token = "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"
    assert Redactor().text(f"x-{token}") == "x-[JWT]" and Redactor().text(f"eyJ-{token}") == "[JWT]"
    assert Redactor().text("eyJhbGciOiJ-eyJh.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4fw") == "[JWT]"
    assert Redactor().text(f"a.{token}") == "a.[JWT]"


def test_an_allow_pattern_sees_the_whole_name_or_url():
    # the regexes start at the secret word and at "://", but an allow pattern is written for what a person reads
    assert "fake-value" not in _from_toml(allow=["password=.*"]).text("db_password=fake-value")
    assert _from_toml(allow=["password=.*"]).text("password=public") == "password=public"
    assert _from_toml(allow=["db_password=.*"]).text("db_password=public") == "db_password=public"
    allowed = "https://ci:public@"
    assert _from_toml(allow=[allowed]).text(f"{allowed}localhost/x") == f"{allowed}localhost/x"
    assert "fake-value" not in _from_toml(allow=[allowed]).text("https://ci:fake-value@example.test")


def test_exclude_fields_names_a_field_in_any_spelling_but_not_a_part_of_one():
    red = _from_toml(profile="pii", sensitive_fields=["order_note"], exclude_fields=["order_note", "phone", "id"])
    assert not red.is_sensitive_field("orderNote") and not red.is_sensitive_field("order-note")
    assert red.is_sensitive_field("phone_number") and red.is_sensitive_field("session_id")


def test_field_names_ignore_any_separator():
    red = _from_toml(profile="pii")
    assert all(red.is_sensitive_field(n) for n in ("first@name", "first:name", "first/name", "first name"))


def test_profile_is_a_base_the_file_extends_and_exclude_wins():
    red = _from_toml(profile="pii", sensitive_fields=["order_note"], exclude_fields=["phone"])
    assert red.is_sensitive_field("order_note") and red.is_sensitive_field("first_name")
    assert not red.is_sensitive_field("phone")


def test_profile_unknown_name_lists_the_choices():
    from graylog_mcp.config import ConfigError

    with pytest.raises(ConfigError, match=r"unknown profile 'nope'.*basic"):
        _from_toml(profile="nope")
    with pytest.raises(ConfigError, match="unknown profile"):
        _from_toml(profile=["pii"])
