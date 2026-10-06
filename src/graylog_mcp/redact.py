"""Masking of sensitive data before any log text leaves the machine.

Three layers:

* core rules, always on: sensitive field names, emails, Bearer/Basic
  credentials, JWTs, ``key=value`` / ``"key": "value"`` secrets, credentials in
  URLs and card numbers that pass the Luhn check;
* country packs, enabled in config (``vn``, ``us``, ``eu``, ...);
* custom regexes from config.

An allow-list of regexes protects values that must never be masked (a match
that fully matches an allow pattern is kept as is).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from graylog_mcp.config import RedactionConfig

REDACTED = "[REDACTED]"

_SECRET_WORDS = (
    r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
    r"client[_-]?secret|auth(?:orization)?|credentials?|cookie|session[_-]?(?:id|key|token)|"
    r"x[_-]api[_-]key|signature|otp|pin[_-]?code"
)
# Field names whose whole value is masked. Matched against the field name, case-insensitive.
_SENSITIVE_FIELD = re.compile(
    r"(?i)(^|[_.\-])("
    r"pass(word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
    r"client[_-]?secret|authorization|auth[_-]?token|credentials?|cookie|set[_-]?cookie|"
    r"session[_-]?(id|key|token)|jsessionid|x[_-]api[_-]key|otp|pin[_-]?code"
    r")($|[_.\-])|^(password|passwd|token|secret|authorization|cookie)",
)
# camelCase names (accessToken, clientSecret, userPassword)
_CAMEL_SENSITIVE = re.compile(r"(Password|Passwd|Secret|Token|ApiKey|AccessKey|PrivateKey|Cookie|Authorization)$")


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    replace: Callable[[re.Match[str]], str]
    validate: Callable[[str], bool] | None = None
    # Lower runs first. Loose patterns (phone numbers, bare 9-digit ids) run last so that
    # validated, more specific ones (IBAN, card) win on overlapping text.
    priority: int = 50


def _const(text: str) -> Callable[[re.Match[str]], str]:
    return lambda _m: text


def _mask_value(value: str) -> str:
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[0] + REDACTED + value[0]
    return REDACTED


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Issuer prefixes: Visa 4, Mastercard 51-55/22-27, Amex 34/37, Diners 30/36/38, JCB 35, Discover/UnionPay 6.
# Restricting the prefix keeps timestamps (19xx/20xx...) and epoch millis (1xxx) from being masked.
_CARD_PREFIX = re.compile(r"^(4|5[1-5]|2[2-7]|3[0-8]|6)")


def _card_ok(raw: str) -> bool:
    digits = re.sub(r"[ -]", "", raw)
    return 13 <= len(digits) <= 19 and bool(_CARD_PREFIX.match(digits)) and luhn_ok(digits)


def _iban_ok(raw: str) -> bool:
    iban = raw.replace(" ", "").upper()
    if not 15 <= len(iban) <= 34:
        return False
    rearranged = iban[4:] + iban[:4]
    try:
        number = int("".join(str(int(c, 36)) for c in rearranged))
    except ValueError:
        return False
    return number % 97 == 1


CORE_RULES: tuple[Rule, ...] = (
    # PEM private keys (multi-line)
    Rule(
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
        _const("[PRIVATE_KEY]"),
    ),
    # credentials embedded in URLs: scheme://user:pass@host
    Rule(
        "url_credentials",
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]*):([^\s@/]+)@"),
        lambda m: f"{m.group(1)}:{REDACTED}@",
    ),
    Rule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
        _const("[JWT]"),
    ),
    Rule(
        "bearer",
        re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9\-._~+/]{8,}=*)"),
        lambda m: f"{m.group(1)} {REDACTED}",
    ),
    Rule(
        "basic_auth",
        re.compile(r"(?i)\b(basic)\s+([A-Za-z0-9+/]{8,}={0,2})(?![A-Za-z0-9+/=])"),
        lambda m: f"{m.group(1)} {REDACTED}",
    ),
    # password=x  token: x  "api_key": "x"  'secret'='x'  (query strings, logfmt, JSON, headers)
    Rule(
        "kv_secret",
        re.compile(
            r"(?i)(?<![A-Za-z0-9])([\"']?)((?:[a-z0-9]+[_.\-])*(?:access|refresh|id|auth|csrf|xsrf|bearer)?"
            r"[_\-]?(?:" + _SECRET_WORDS + r"))\1(\s*[=:]\s*)(?!\[[A-Z_]+\])"
            r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s&,;\"'<>)\]}]+)"
        ),
        lambda m: m.group(1) + m.group(2) + m.group(1) + m.group(3) + _mask_value(m.group(4)),
    ),
    Rule(
        "email",
        re.compile(r"(?i)(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}\b"),
        _const("[EMAIL]"),
    ),
    Rule(
        "card",
        re.compile(r"(?<![\d\-])(?:\d[ -]?){12,18}\d(?![\d\-])"),
        _const("[CARD]"),
        _card_ok,
    ),
)


def _vn_rules(cfg: RedactionConfig) -> list[Rule]:
    rules = [
        # CCCD: 12 digits starting with the 0xx province code
        Rule("vn_cccd", re.compile(r"(?<![\d.])0\d{11}(?![\d.])"), _const("[VN_ID]")),
        # mobile 03/05/07/08/09 + 8 digits, landline 02x + 8 digits; +84 / 84 / 0 prefix, optional separators
        Rule(
            "vn_phone",
            re.compile(r"(?<![\d+])(?:\+?84[ .\-]?|0)(?:[35789]\d|2\d{2})(?:[ .\-]?\d){7}(?![\d])"),
            _const("[PHONE]"),
            priority=90,
        ),
    ]
    if cfg.vn_cmnd:
        rules.append(Rule("vn_cmnd", re.compile(r"(?<![\d.])\d{9}(?![\d.])"), _const("[VN_ID]"), priority=95))
    return rules


def _us_rules(_cfg: RedactionConfig) -> list[Rule]:
    return [
        Rule(
            "us_ssn",
            re.compile(r"(?<!\d)(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?!\d)"),
            _const("[SSN]"),
        ),
    ]


def _eu_rules(_cfg: RedactionConfig) -> list[Rule]:
    return [
        Rule(
            "iban",
            re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"),
            _const("[IBAN]"),
            _iban_ok,
        ),
    ]


def _uk_rules(_cfg: RedactionConfig) -> list[Rule]:
    return [
        Rule(
            "uk_nino",
            re.compile(r"\b(?![DFIQUV])[A-Z](?![DFIQUVO])[A-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b"),
            _const("[NINO]"),
        ),
    ]


def _in_rules(_cfg: RedactionConfig) -> list[Rule]:
    return [
        Rule("in_aadhaar", re.compile(r"(?<!\d)[2-9]\d{3} ?\d{4} ?\d{4}(?!\d)"), _const("[AADHAAR]")),
        Rule("in_pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), _const("[PAN]")),
    ]


PACKS: dict[str, Callable[[RedactionConfig], list[Rule]]] = {
    "vn": _vn_rules,
    "us": _us_rules,
    "eu": _eu_rules,
    "uk": _uk_rules,
    "in": _in_rules,
}


class Redactor:
    def __init__(self, cfg: RedactionConfig | None = None):
        cfg = cfg or RedactionConfig()
        self._cfg = cfg
        rules: list[Rule] = list(CORE_RULES)
        for pack in cfg.packs:
            rules.extend(PACKS[pack](cfg))
        rules.sort(key=lambda r: r.priority)  # stable: core rules keep their order
        for custom in cfg.patterns:
            rules.append(Rule(custom.name, custom.pattern, _const(custom.replacement)))
        self.rules = tuple(rules)
        self._extra_fields = tuple(f.lower() for f in cfg.extra_sensitive_fields)
        self._exclude_fields = {f.lower() for f in cfg.exclude_fields}

    @property
    def active_rules(self) -> list[str]:
        return [r.name for r in self.rules]

    def is_sensitive_field(self, name: str) -> bool:
        low = name.lower()
        if low in self._exclude_fields:
            return False
        if any(extra == low or extra in low for extra in self._extra_fields):
            return True
        return bool(_SENSITIVE_FIELD.search(name) or _CAMEL_SENSITIVE.search(name))

    def _allowed(self, text: str) -> bool:
        return any(p.fullmatch(text) for p in self._cfg.allow)

    def _apply(self, rule: Rule, value: str) -> str:
        def sub(m: re.Match[str]) -> str:
            matched = m.group(0)
            if rule.validate is not None and not rule.validate(matched):
                return matched
            if self._allowed(matched):
                return matched
            return rule.replace(m)

        return rule.pattern.sub(sub, value)

    def text(self, value: str) -> str:
        if not value:
            return value
        for rule in self.rules:
            value = self._apply(rule, value)
        return value

    def explain(self, value: str) -> tuple[str, list[str]]:
        """Masked text and the names of the rules that changed it."""
        hits = []
        for rule in self.rules:
            changed = self._apply(rule, value)
            if changed != value:
                hits.append(rule.name)
                value = changed
        return value, hits

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, int | float) and not isinstance(value, bool):
            # card, phone or id numbers shipped as numeric fields
            masked = self.text(str(value))
            return value if masked == str(value) else masked
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return self.fields(value)
        return value

    def fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for key, val in fields.items():
            if val is not None and val != "" and self.is_sensitive_field(key):
                out[key] = REDACTED
            else:
                out[key] = self.value(val)
        return out

    def many(self, values: Iterable[Any]) -> list[Any]:
        return [self.value(v) for v in values]
