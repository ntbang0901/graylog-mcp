# Plan: ship the admin UI revamp (v3)

**Goal:** turn the admin page into the ops console described in `ui-demo/admin/DESIGN.md`. That means a new shell
(grouped nav, environment picker, status bar), a new layout for each screen, three colour schemes with Settings,
and keyboard-first interactions. The API, the data and the read-only rules stay the same.

**Approach:**

- **Prototype first,** with static demo data, so the layout can be judged before the 1,750-line page is touched.
- **Then ship in small PRs,** foundation first and one screen at a time. Each PR leaves the page working and passing the four checks.
- **Keep the token names and element IDs** that the JS uses, unless a step says it replaces them.

## Status

- [x] Read the current page, the CSP, the API routes, the usage record and the test that inspects the page
- [x] DESIGN.md: the shell, 8 screens, 3 schemes (contrast checked), motion, keys and storage
- [x] Phases 0–4 below

---

## Phase 0: Prototype (in `ui-demo/admin/`, untracked)

- [x] `admin-demo.html` contains the shell (sidebar groups, top bar with the environment picker, status bar) and
  the **Overview** and **Activity** screens. It uses static demo data shaped like the real API responses.
  Activity was built on 2026-11-12 (chart as filter, Calls / By tool / Learned, expandable rows, J/K/Enter/P).
  Save those responses from a dev run as `demo-data.js`, with neutral names (`acme/prod`, `example.com`).
- [x] Include the scheme and appearance switch, so all eight themes can be compared on real layouts.
- [x] Review Activity with the user (the user approved the prototype screens before the build began, "those lgtm"; the default scheme stays Slate): layout, density and the default scheme. Fold the decisions back into DESIGN.md.

**Exit:** the user approves the shell and the two main screens.

## Phase 1: Foundation (PR 1)

- [x] Branch `admin-ui-v3`.
- [x] **Tokens:**
  - re-key the Slate blocks to `[data-palette=slate][data-theme=…]`, keeping `:root` as the no-JS fallback
  - add the Midnight and Graphite blocks
  - add `--dur-long`, `--font-ui` and `--font-mono` (with aliases), and `--radius-control: 8px`
- [x] **Bootstrap script:** read `gm-palette`, `gm-theme` and `gm-density` before first paint, inside try/catch.
- [x] **Settings `<dialog>`:** scheme cards, Appearance, Density and Shortcuts. Add `setPalette`, `setMode` and `setDensity`, with View Transitions.
- [x] **Shared helpers:** `showAnimated` / `hideAnimated` (fade for the confirm box and the palette; instant under reduced motion),
  `slidePill(group)` for segments, and `onKey(fn, when)` with the guard for typing, open overlays and modifier keys.
- [x] **Segments** get the sliding pill. Buttons press with a scale. Mono is applied to data across all screens.
- [x] Run the four checks. Then a DevTools pass on the current layout in the six themes.

## Phase 2: Shell (PR 2)

- [x] **Sidebar:** the Monitor / Configure / Tools groups. Rename Dashboard to Overview and Other clients to Connect.
  Config file moves out of the nav, but its page `id`s stay so `data-goto="config"` keeps working.
- [x] **Meaningful nav counts:**
  - Activity: problem calls, from `/api/usage`
  - Environments: failing count, from `/api/doctor`
- [x] **Top bar:**
  - breadcrumb
  - **environment picker** (a combobox over `/api/state` instances, with health from the doctor results)
  - kbar, ⚙ and theme toggle
  - the page title and subline move into each screen's head
- [x] **Environment context:**
  - `gm-env` with a `CTX_ENV` global and an `envchange` event
  - the screens subscribe in later PRs
- [x] **Status bar:** the server state (`/api/setup`), the config path that links to raw config, the config status
  badge (moved from the top bar), the version and the unsaved mirror.
- [x] **Palette:** add the screens under their new names, "Switch environment: …", the Settings, Scheme and
  Appearance entries, and "Open raw config".
- [x] **Keys:** `/`, `E`, `,`, `T`, `1/2/3`, and `G`+letter.

## Phase 3: Screens (one PR each, in this order)

### 3a. Overview

- [x] A status line from `/api/state`, `/api/doctor` and `/api/usage` totals.
- [x] **Needs attention:** merge the failing environments, failed doctor checks, setup steps (from `renderSetup`)
  and misses. Each item has one action, and the row collapses when its fix succeeds. Remove the old setup card and
  `#otherChecks`, and move their actions here.
- [x] **Ledger:** add the totals and error counts from `/api/logstats`, and highlight the context environment.
- [x] **KPI rail:** show each figure's change against `previous`.
- [x] **States:** loading (a skeleton per block), empty (`// all clear`), and an error (one environment
  unreachable must not hide the others, as `/api/logstats` already ensures).
- Not done: a p95 latency tile (`/api/usage` has per-tool p95 only). Tool bars and the tool-call tiles do not narrow to the
  environment, because `/api/usage` has no instance filter (DESIGN §10); the page says "all envs".

### 3b. Activity

- [x] Replace the cards with: a filter bar, the stacked chart (click a bucket to filter), and the Calls / By tool / Learned tabs.
- [x] **Log list over `recent`:**
  - rows built once; filtering hides rows with the collapse, it does not re-render them
  - an expanded row shows `args`, `params`, `requests`, `returned` and `truncated`
- [x] **Open in Playground:** pre-fills the env, tool and args, merging `args` and `params`. Copy as JSON.
- [x] Keys: `J` / `K` / `Enter` / `P`. The environment context filters the list.
- Not done: the chart and By tool counts do not narrow to the environment (`/api/usage` has no instance filter, DESIGN §10); the page says so.
  "Edit token" is an "Edit <env>" button on failed calls of failing local environments.

### 3c. Playground

- [x] **Split layout:** request on the left, result on the right. They stack at ≤75rem.
- [x] **Args editor:** a line-number gutter, JSON validation on blur, and `Tab` indentation.
- [x] **Result:** a Pretty view (collapsible JSON) and a Raw view, with a mono meta line (ms, size, masked count).
- [x] **Session history:** the last 20 runs, kept in memory. It receives "Open in Playground" from Activity.
- [x] Keep `tool_templates` and `quick_tools` coming from the server, so the test still passes.
- Note: "masked count" counts the redactor's marks (`[EMAIL]`, `[CARD]`, ...) in the result text; `/api/run` does not report one.

### 3d. Environments

- [x] Group sections: a head (description, default env), the env table, then the repositories as a sub-list.
- [x] **The drawer becomes a `<dialog>`** with Connection / Check / Fields tabs. Keep the `f_*` IDs and the save and test flow.
- [x] Show the test result inline in the row.
- Not done: a latency column (`/api/doctor` and `/api/test` report none). The row shows the test time, version and stream count instead.
- Extra: each local row has a Test button, and `openFieldsFor(scope)` opens Log fields at one level (3e reuses it).

### 3e. Log fields

- [x] The scope breadcrumb replaces `#s_scope`; it can stay as a hidden select if the JS needs it.
- [x] Each field gets a provenance tag ("from X", or "set here ✕" to clear that level).
- [x] Detect moves to a sticky right column, defaulting to the context environment. After Apply, highlight the changed fields.
- Note: the breadcrumb shows the chain of the level being edited; the old select stays beside it as "Other level" so any group or
  environment can still be picked. The provenance tag is worked out in the page from `/api/scope` of each ancestor level.

### 3f. Redaction

- [x] An accordion of the settings sections.
- [x] A before/after "Try it", with masked tokens highlighted (a client regex over the `[…]` placeholders) and the count in mono.
- Not done: a hover that says which rule masked each token (`/api/redact` returns rule names, not where each applied; DESIGN §10).
  The count line shows the number of masked tokens and the rules that changed the text. Custom pattern replacements are marked too.

### 3g. Connect and Config file

- [x] Connect: a tab per client, each a three-step stack (source, snippet, install).
- [x] Config: a line-number gutter, Validate on `Ctrl S`, reached from the status bar, the palette and the Configure tabs.
- Note: Claude Code is the first tab and carries the "every project" command; the config error line is marked only when the message names one
  (TOML syntax errors do, validation errors usually do not). A "Raw file" button sits in the head of Environments, Log fields and Redaction.

## Phase 4: Verify and wrap up (with every PR, in full after 3g)

- [x] `uv sync && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest`.
- [x] **Chrome DevTools against `graylog-mcp ui`,** using a config that points at `tests/fake_graylog.py`:
  - [x] No console errors and no CSP violations.
  - [x] 3 schemes × 2 themes × 8 screens (four schemes were built: Slate, Midnight, Graphite, Catppuccin):
    - all 64 combinations checked by script for horizontal overflow, empty screens, script errors and AA text contrast
      (33,666 text nodes, no failure);
    - the colour tokens, page background and text colour read from the real page and from the prototype in all 8 themes are identical;
    - screenshots of the real Overview next to the prototype's in Slate dark (also Activity), Midnight dark, Graphite light and
      Catppuccin dark: the layout and colours match;
    - the shell (sidebar, top bar, status bar, nav item, controls) compared with the prototype on 8 screens x 8 themes: the sidebar,
      status bar and active nav item are identical on all 8 screens and equal the prototype's in every combination (pass, table in
      DESIGN.md §11), with no script errors on the 64 runs; the remaining small
      differences are listed in DESIGN.md §11;
    - `admin-demo.html` builds only Overview and Activity (its screen switch sends every other screen to a "Not built in this prototype"
      placeholder), so the content of the other six screens cannot be compared with the prototype. They were checked by the scripts
      above and in screenshots while being built (Catppuccin light, Log fields).
  - [x] States per screen: loading, empty, error and full. Forced 500s on every screen that loads its own data (Overview,
    Activity, Environments, Config, Redaction, Log fields, Connect, and a failed Playground run): each shows a message and none
    leaves an unhandled rejection. Delayed responses checked on Log fields, Config and Connect. Environments and Redaction draw from
    the state loaded at start, so their loading is the page's first paint. Empty states: Overview and Activity.
    Also unsaved changes with the savebar, and navigating away with unsaved edits (Config, Redaction, Log fields).
  - [x] Environment context: set, reload, then delete that environment, and check it falls back to All.
  - [x] Storage: an empty store gives Slate + System + Comfortable + All. A stored `gm-theme=dark` is kept. Blocked storage renders the defaults.
  - [x] Reduced motion: no View Transitions, and the confirm box and palette hide at once (checked in the browser).
  - [x] Widths of 375px, 768px and 1440px: no horizontal page scroll, and tables restack.
  - [x] Keyboard: every shortcut, Tab order, visible focus in the six themes, and focus returns after each overlay.
  - [x] Lighthouse accessibility on all eight screens in both light and dark (16 combinations): 100 in all four categories. It found
    two real issues, fixed: Connect's step headings skipped a level (now `h2`), and the status bar config link was a 17px target
    (now 24px). Overview (light) and Activity (dark) were last audited before those two fixes and the helper edits; the other
    fourteen after.
- [x] **Motion (§8):** the screen switch fades in and rises 4px, and the Overview figures count up over 480ms once per range. Verified: opacity 0.4 at 30ms and 1 at 300ms with `transform` none after, the Playground sticky card still sticky, reduced motion shows 1 at once, and a Refresh does not replay the count.
- [x] **CHANGELOG under Unreleased, one line per PR,** for example: "The admin page is reorganised as a console:
  Overview leads with what needs attention, Activity is a filterable call log that opens any call in the
  Playground, and an environment picker in the top bar carries one environment across screens."
- [x] **README:** update the admin page section, including the screen names, Settings and the shortcuts.
- [x] Update DESIGN.md with anything that changed (§11). Decide whether `ui-demo/admin/` moves to `docs/design/`.

---

## Phase 5: Move the page to Astro (split output)

**Status (2026-11-18):** 5a, 5b and 5f are done and verified: all 56 screen × scheme × mode combinations have the same
text and computed styles as the old page (Chrome, same fake data), `npm run dev` proxies `/api`, the build is
reproducible. 5c (split by screen), 5d (types) and 5e (no `'unsafe-inline'` for styles) are done too. 5d left 283 untyped
parameters (`noImplicitAny` is off) and DOM lookups typed `any`: tighten both later. Differences from the plan below: the CSP hash is computed from the built page at startup (no hard-coded hash); `/_astro/{name}` is a small
route in `app.py` instead of `StaticFiles`; the favicon is `ui/public/favicon.svg` behind its own route.

**Goal:** the same page, built from an Astro project instead of one hand-edited 3,000-line file. No visual or
behaviour change in 5a–5c; the admin API stays as it is. This replaces DESIGN §0 "One file".

**Layout:**

```
ui/                              Astro 7 project, npm (package.json, package-lock.json, astro.config.mjs, tsconfig.json)
  public/                        copied as-is, never bundled: favicon.svg only
  src/pages/index.astro          the shell: head, sidebar, top bar, status bar, confirm box, toasts
  src/components/<Name>.astro    the 8 screens (Overview, Activity, Environments, LogFields, Redaction, Playground,
                                 Connect, ConfigFile) and the Settings, EnvDrawer and Palette dialogs
  src/styles/                    main.css lists the parts in cascade order: tokens.css (4 schemes × light/dark,
                                 density), layout, controls, tables, charts, overlays, shell, one file per screen
  src/scripts/                   main.ts (start), core.ts (api, toast, shared state: imports nothing), theme.ts,
                                 shell.ts (navigation, unsaved changes, keys, status bar, environment picker),
                                 charts.ts, palette.ts, one module per screen
```

- `outDir` is `../src/graylog_mcp/admin/static` and `build.assets` stays `_astro`, so the build writes
  `index.html` + `_astro/*.[hash].js|css`. **The build output is committed**, so the wheel, Docker, CI's Python jobs
  and `uv sync && pytest` still need no Node.
- Scripts and styles go under `src/` (bundled, typed, hashed), not `public/`: `public/` files skip Vite entirely.
- The theme-before-paint script stays `<script is:inline>` in `<head>`; it is the only inline script.

**Steps:**

- [x] **5a. Scaffold.** `ui/` with Astro, `astro check`, `.gitignore` for `ui/node_modules`. `index.astro` holds the
  current markup, `tokens.css`/`base.css` the current CSS, `src/scripts/main.ts` the current JS (`// @ts-nocheck`
  at first). The build output matches today's page; check that in Chrome.
- [x] **5b. Serve the split page** (`admin/app.py`): mount `/_astro` with
  `StaticFiles(packages=[("graylog_mcp.admin", "static/_astro")])`, `Cache-Control: public, max-age=31536000,
  immutable` there (hashed names), `no-store` on `/` as now. CSP: `script-src 'self' 'sha256-<head script>'`, with no
  `'unsafe-inline'`. A test computes the hash from the built `index.html` so they can't drift.
  Update `test_the_admin_page_keeps_no_operation_list_of_its_own` to read the `ui/src` sources (or the built JS).
- [x] **5c. Split by screen.** Each screen's markup is a component and its script a module; the CSS is cut into
  contiguous files that `main.css` imports in the old order. Done in one pass, not one commit per screen, because the
  result can be compared exactly: the built `index.html` and CSS are byte-identical to the unsplit build (same
  `_astro/index.<hash>.css` name), and all 7 other screens × 4 schemes × 2 modes match on text, computed styles and
  HTML in Chrome (Overview differs only in chart data and count-up timing).
- [x] **5d. Types.** `@ts-nocheck` is gone from every module; `types.ts` types the `/api/*` responses, `tsc` and
  `astro check` report 0 errors. Not strict yet: `noImplicitAny` is off (283 parameters) and `dom.d.ts` makes DOM
  lookups `any`. Runtime compared with the pre-split build: same.
- [x] **5e. Tighten styles.** Static `style="…"` became classes (`svg.i.xs`, `.glabel.gap`, `.pv i.s.short`,
  `.pop li.empty-opt`); figures that come from data are `data-pct` / `data-grow` / `data-bg`, applied by a
  `MutationObserver` in `core.ts` through the CSSOM. `style-src` and `img-src` are `'self'` only (the favicon is a
  file). A test fails on any `style=` in `ui/src`. Compared with the previous build: same text and computed styles on
  all screens (Home and Activity differ only in time-shifted fake data), console clean.
- [x] **5f. CI and docs.** A `ui` job: `npm ci && npm run check && npm run build && git diff --exit-code
  src/graylog_mcp/admin/static`. Dependabot for `/ui` (npm). CONTRIBUTING: how to run `npm run dev` against a running
  `graylog-mcp ui` (Vite `server.proxy` for `/api`). CHANGELOG line. DESIGN §0 updated.

**Risks:** the dev server has to get past the admin app's Host check and token. Proxy `/api` with
`changeOrigin: true` so Host is the admin app's own, and open `http://localhost:4321/#token=<printed token>`: the page
reads the token from the fragment and sends `x-admin-token` as now; stale committed build (the CI diff check catches it); the dev-harness
hazard above still applies when driving the page.

## Risks

- **Size.** The page is about 150KB already. Budget about +25KB in total. Share CSS across the schemes and
  delete the code each screen replaces in the same PR.
- **Hidden-screen measurement.** Pills and gutters measure as 0 inside `display:none`. Measure them on activation.
- **Activity performance.** `recent` holds up to 200 rows: build them once and filter with classes. If it ever grows,
  render only the rows in view.
- **Losing unsaved edits.** The new navigation (the picker, `G`+letter, the status bar link) must go through the
  same unsaved-changes guard as the nav buttons.
- **Merge conflicts.** Any concurrent edit to `index.html` will conflict. Land the PRs quickly, one after another.

## Decisions for the user

1. Default scheme: **Slate** (continuity) or Midnight.
2. The Phase 0 prototype: build it first (recommended), or start straight on PR 1.
3. The §10 server follow-ups: rule names in the redaction output, and line numbers in config validation. Include them, or leave them for later.
4. Where the design docs live: they stay local in `ui-demo/`, or are committed under `docs/design/`.

## Phase 6: Motion library (2026-11-18)

- [x] Added `motion` 14.14.0 (exact pin, full entry: the page is local, so the size does not matter; `motion/mini` was tried
  first and added 12KB instead of 75KB). `ui/src/scripts/motion.ts` holds the springs and every script animation: screen
  switch, Activity rows, confirm box and palette, toasts (enter and exit), the segmented-control pill, the scheme-card pop,
  the figure count-up, chart draw-in. CSS `.swap`, `fadein` on overlays/toasts and the pill `transition` are removed.
- [x] Verified in Chrome against the harness, tab in front: stagger on screen switch and Activity rows, no leftover inline
  style after any of them, palette centred without a transform, toasts leave and close the gap, the pill lands on its button,
  the line traces and the figures count, bars grow, reduced motion shows everything at once.
- **Gotchas.** One `animate()` over a list resolves `finished` before its last element commits its end style, so every list
  animation is one `animate()` per element (`each()`). A background tab pauses the script-driven parts (count-up, line trace);
  bring the tab to the front before measuring. `navigate_page` to the same URL with a new hash does not reload: use `reload`.
- [ ] Ideas left: row insert/remove FLIP, drawer spring.


## Phase 7: Preact + signals (2026-11-18)

- [x] Scaffold Vite + `@preact/preset-vite` alongside the existing screens and bridge shared state through signals.
- [x] Port the shell, settings, environment context, routing, unsaved-changes guard and shortcuts.
- [x] Port Overview, its breakdown, logs drawer, SVG charts and donut.
- [x] Port screens individually: Activity; Environments and editor; Log fields; Redaction; Playground; Connect;
  Config file; command palette. Keep JSX handlers, existing CSS/class names and the same API.
- [x] Remove Astro and imperative renderers, style MutationObserver, delegated clicks and permissive DOM types.
- [x] Enable strict TypeScript and `tsc --noEmit`. Pin dependencies exactly; build external relative assets into
  `admin/static/assets/` with `assetsInlineLimit: 0` and the same `/api` development proxy.
- [x] Finish the safe FakeGraylog harness comparison at 1440px and 390px, all four schemes × light/dark,
  interactions, API-error states, CSP, reduced motion and both mount paths. Record results in the session report.
- [x] Complete the Python checks and final `npm run check` / `npm run build`; Prettier `format:check` and four store regression tests also pass.
- [x] Fix hover tooltip and environment-picker positioning with explicit CSS pixel units. Portal chart tooltips
  outside animated containers; wait one frame before measuring screen entrances. Verify 20 tooltip/picker
  cases across both viewport sizes and mount paths, including transformed containers and keyboard selection.
- Verification: 128 screen × scheme × appearance × viewport cases, no overflow, browser errors or CSP violations;
  the sampled styles match apart from a 0.015px Run-button width difference. Overlay panel geometry matches too.
  Keyboard/dialog/save flows and API error, delayed, empty, blocked-storage and reduced-motion cases pass.
  Python: Ruff and format pass, mypy reports no issues in 47 files, pytest reports 650 passed / 3 deselected.
