"""Error candidates for a described error: the whole range, exact counts where a field allows."""

from __future__ import annotations

from graylog_mcp.ask.candidates import error_candidates, keywords_of
from graylog_mcp.client import requests_sent
from graylog_mcp.tools import _range
from tests.test_trace import _add


def _errors(fake, lines, seconds_ago, message, spread=1.0, **fields):
    for i in range(lines):
        _add(fake, seconds_ago - i * spread, level=3, service="payment", message=f"{message} {i}", **fields)


async def _candidates(app, keywords=(), span="6h", limit=30):
    gl = app.gl(None)
    return await error_candidates(app, gl, _range(gl, span, None, None, span), gl.cfg.error_query, keywords, 250, limit)


async def test_an_error_early_in_the_range_is_a_candidate(make_app):
    app, fake = make_app()
    fake.messages = []
    _errors(fake, 600, 290, "payment timeout", spread=0.4)
    _errors(fake, 3, 5 * 3600, "ledger mismatch")
    found = await _candidates(app)
    texts = [c.text for c in found.groups]
    assert any("ledger mismatch" in t for t in texts) and any("payment timeout" in t for t in texts)
    assert found.total == 603 and found.sampled < 603


async def test_counts_are_exact_when_the_exception_field_exists(make_app):
    app, fake = make_app()
    fake.messages = []
    _errors(fake, 600, 290, "bank call failed", spread=0.4, exception_class="java.net.SocketTimeoutException")
    _errors(fake, 4, 3 * 3600, "row locked", exception_class="org.acme.LockError")
    found = await _candidates(app)
    timeout = next(c for c in found.groups if c.exception == "java.net.SocketTimeoutException")
    assert found.exact and timeout.count == 600 and timeout.seen < 600 and found.estimate(timeout) == 600
    assert timeout.first and timeout.last and timeout.service == "payment"
    assert next(c for c in found.groups if c.exception == "org.acme.LockError").count == 4


async def test_without_the_exception_field_counts_are_estimates(make_app):
    app, fake = make_app()
    fake.messages = []
    _errors(fake, 600, 290, "payment timeout", spread=0.4)
    found = await _candidates(app)
    (only,) = found.groups
    assert not found.exact and only.count is None
    assert found.estimate(only) == only.seen * found.total // found.sampled == 600


async def test_an_exception_only_the_aggregation_saw_is_still_a_candidate(make_app):
    app, fake = make_app(limits={"max_limit": 60, "default_limit": 50})
    fake.messages = []
    _errors(fake, 400, 290, "bank call failed", spread=0.01, exception_class="java.net.SocketTimeoutException")
    # older than the four hundred lines of its own bucket, of which only the newest ten are read
    _errors(fake, 2, 295, "ledger out of balance", spread=0.001, exception_class="org.acme.LedgerError")
    found = await _candidates(app, span="1h")
    rare = next(c for c in found.groups if c.exception == "org.acme.LedgerError")
    assert rare.sample == {} and rare.count == 2 and "org.acme.LedgerError" in rare.text


async def test_keywords_put_a_rare_group_first(make_app):
    app, fake = make_app()
    fake.messages = []
    _errors(fake, 600, 290, "payment timeout", spread=0.4)
    _errors(fake, 3, 5 * 3600, "ledger mismatch")
    plain, keyed = await _candidates(app), await _candidates(app, keywords=["Ledger", "missing"])
    assert "payment timeout" in plain.groups[0].text
    assert "ledger mismatch" in keyed.groups[0].text and keyed.groups[0].hits == 1


async def test_fewer_buckets_when_requests_run_out(make_app):
    app, fake = make_app()
    fake.messages = []
    _errors(fake, 50, 290, "payment timeout")
    await _candidates(app)  # version and field list are read once per instance, not per question
    sent = [17]
    requests_sent.set(sent)
    found = await _candidates(app)
    assert found.groups and sent[0] - 17 <= 4


async def test_a_refused_aggregation_leaves_the_samples(make_app):
    app, fake = make_app(investigation={"group_fields": {"exception": "message"}})
    fake.messages = []
    _errors(fake, 20, 290, "payment timeout")
    found = await _candidates(app)
    assert found.groups and found.total == 20


async def test_at_most_nine_requests(make_app):
    app, fake = make_app()
    fake.messages = []
    _errors(fake, 50, 290, "payment timeout", exception_class="java.net.SocketTimeoutException")
    await _candidates(app)
    sent = [0]
    requests_sent.set(sent)
    await _candidates(app)
    assert sent[0] <= 8  # the count, the aggregation and six samples


async def test_a_range_one_read_holds_is_read_whole(make_app):
    app, fake = make_app()
    fake.messages = []
    _errors(fake, 150, 290, "payment timeout")
    _errors(fake, 7, 1000, "deadlock detected")  # older than the newest hundred, in the same part of the range
    found = await _candidates(app)
    assert found.sampled == found.total == 157 and {c.seen for c in found.groups} == {150, 7}


async def test_no_errors_no_candidates(make_app):
    app, fake = make_app()
    fake.messages = []
    found = await _candidates(app)
    assert found.groups == [] and found.total == 0


def test_keywords_of_a_question():
    question = "payment gateway timeout hôm qua có nhiều không"
    assert keywords_of(question, [(24, 31)], ["refund"]) == ("refund", "payment", "gateway", "timeout")
    assert keywords_of("What errors does Payment have with ORD-1001", [(35, 43)], []) == ("payment",)
    assert keywords_of("nghiệp vụ hoàn tiền", [], []) == ()  # no part of an accented word is a keyword
