# Security policy

graylog-mcp sits between log data and a language model, so two kinds of problems are security issues:

- **Masking gaps**: a sensitive value (credential, card number, personal identifier) that reaches tool output
  although a rule should have masked it.
- **Write paths**: any way to make the server change state in Graylog. It must only send `GET` requests and
  `POST` to the read-only search endpoints listed in `client.py` (`READ_ONLY_POSTS`).

Also in scope: secrets leaking through error messages or logs, and authentication bypass of the HTTP
transport.

## Reporting

Please report privately through
[GitHub security advisories](https://github.com/ntbang0901/graylog-mcp/security/advisories/new), not in a
public issue. Include a minimal example with **synthetic** data (never real log lines or tokens).

You can expect an acknowledgement within a week. Fixes are released as patch versions and credited in the
changelog unless you prefer otherwise.

## Supported versions

Security fixes go to the latest minor release.
