# Contributing

## Setup

```bash
uv sync                     # Python 3.11+, installs dev tools from uv.lock
uv run --only-group lint pre-commit install   # optional: ruff and mypy on every commit
```

## Checks

```bash
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest               # unit + contract tests, coverage must stay >= 85%
```

Changes to `backends/`, `client.py`, query building or `rca.py` should also pass the integration suite
against real Graylog containers (needs Docker, about 3 GB of RAM per version):

```bash
tests/integration/run.sh v52        # v43 v50 v52 v61 v70
```

CI runs on every pull request and every push to `main`: lint, mypy and tests (Linux on 3.11-3.13, plus
Windows and macOS), a build and smoke test of the wheel and of the Docker image (amd64 and arm64), a scan
of the image for fixable high-severity vulnerabilities, `pip-audit` on the runtime dependencies, and
actionlint, zizmor and the pre-commit hygiene hooks on the repository. The same tools run locally:

```bash
uv run --only-group lint actionlint
uv run --only-group lint zizmor .github/workflows
```

`main` only accepts pull requests whose "CI passed" check is green. Actions are pinned by commit SHA with the
version in a comment; Dependabot keeps them, the Python dependencies and the base images current, and its
minor and patch updates merge on their own once CI passes. The integration matrix runs weekly and on pull
requests that touch the backends.

## The admin page

The page behind `graylog-mcp ui` is built from `ui/` (Vite, Preact, TypeScript and `@preact/signals`). Its build is
committed in `src/graylog_mcp/admin/static/`, so Python work, the wheel and the tests need no Node. Edit `ui/src`,
never the build. You need Node 22.23.4 or newer:

```bash
cd ui
npm ci
npm run build               # writes ../src/graylog_mcp/admin/static; commit the result with the source
npm run check               # tsc --noEmit, with strict and noImplicitAny enabled
npm run test                # store import, computed state, navigation guard and request races
npm run format              # Prettier for UI source/config; keeps the existing CSS/assets untouched
npm run format:check        # checks the same files without changing them
```

`ui/src/components/` has one `.tsx` component per screen, plus the shell and overlays. Shared state and derived
values live in `ui/src/store.ts` as signals and computeds; environment changes update subscribers directly.
`api.ts` handles token storage and requests at both `/` and `/admin/`. Browser initialization runs from the
entry point and component effects; shared modules do no DOM work at import time. Keep the API types in
`ui/src/scripts/types.ts` in step with `admin/app.py`.

`ui/src/styles/main.css` imports the global CSS parts in cascade order: keep that order and the existing class
names. Animation goes through `ui/src/scripts/motion.ts`, which checks reduced motion. Native dialogs open and
close through refs in effects. The CSP allows only external scripts/styles and the hash of the theme bootstrap
in `ui/index.html`. Use JSX text and handlers. Data-driven sizes and colours use `style={{…}}`, which Preact
applies through the CSSOM; never put style attributes in HTML or build HTML strings. Vite keeps CSS and JS in
external files under `assets/` with relative links, so both admin mount paths work.
Preact 11 requires explicit units for lengths in style objects (``left: `${x}px` ``); unitless properties such as
`flex` and `opacity` can stay numeric.

CI rebuilds the page and fails when the committed build differs from `ui/`. To work with live reload, run
`graylog-mcp ui` in one terminal and `npm run dev` in another: the dev server forwards `/api` to the admin
server (set `ADMIN_URL` to its address, default `http://127.0.0.1:8765`) and the page is
`http://localhost:4321/#token=<the token printed by graylog-mcp ui>`.

## Rules of the project

- **Read-only.** Never add a request that changes Graylog state. New `POST` endpoints must execute or check
  a query without storing anything and be added to `READ_ONLY_POSTS` with a comment saying why.
- **Everything shown is redacted and budgeted.** Tool output goes through `Redactor` and the output budget.
  Add a test with a planted sensitive value for new output paths.
- **Site-specific values belong in config**, not in code.
- **Tests come with fixes.** For behaviour that differs between Graylog versions, extend
  `tests/fake_graylog.py` with the real response shape and note the version.
- Update `CHANGELOG.md` under "Unreleased".

## Releasing

1. In a pull request, move the "Unreleased" entries to a new version section in `CHANGELOG.md` and bump `__version__` in
   `src/graylog_mcp/__init__.py` and `version` in `pyproject.toml`.
   CI fails a pull request where the three disagree or the version has no `CHANGELOG.md` section.
2. Merge the pull request. The release workflow sees a version without a `vX.Y.Z` tag, runs the full CI on
   the merge commit, publishes the wheel and sdist CI smoke-tested to PyPI (trusted publishing with
   attestations), then signs them with Sigstore, creates the GitHub release with the `vX.Y.Z` tag and pushes
   the Docker image (amd64 and arm64) to GHCR. Do not push tags yourself; `v*` tags cannot be moved or deleted.
