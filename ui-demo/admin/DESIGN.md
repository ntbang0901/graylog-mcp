# graylog-mcp admin — UI revamp (v3)

A revamp of the local admin page (`src/graylog_mcp/admin/static/index.html`, opened by `graylog-mcp ui`).

- **What changes:** the shell, the layout of every screen, the visual system and the interactions.
- **What stays:** the API, the data and the job of the page.

The page goes from a settings site with charts to an **ops console**: it answers "is everything working, and what
did the model do?" first, then lets you fix it in place.

The process mirrors `ui-demo/DESIGN.md` and `dev-demo.html` (the Still revamp). This file adapts it to a page with
hard constraints the Still page did not have.

## 0. Constraints (do not break)

- **Built from `ui/`.** The page uses Vite + Preact + `@preact/signals` (PLAN phase 7), with strict TypeScript.
  Its committed build is `admin/static/index.html` plus hashed external files in `assets/`, linked relatively so
  both `/` and `/admin/` work. Edit `ui/src`, never the build. The only inline script is the colour-scheme bootstrap
  in `<head>`; the CSP allows it by hash. JSX styles use the CSSOM; built HTML contains no style attribute.
- **CSP** (`admin/app.py` `_security_headers`: scripts `'self'` plus the hash of the inline one; styles and images `'self'`; no style attributes): no web fonts, CDNs or remote images. The page uses system font stacks only.
- **No new API.** Every screen below uses existing endpoints only:
  - `/api/state`
  - `/api/doctor`
  - `/api/usage`
  - `/api/logstats`
  - `/api/learned`
  - `/api/scope`
  - `/api/detect`
  - `/api/redact`
  - `/api/run`
  - `/api/clients`
  - `/api/config`
  - `/api/setup`
  - the save routes

  Anything that would need a new field is listed in §10.
- **Read-only toward Graylog.** The UI adds no call that writes to Graylog. It cannot, because `client.py` only reads.
- **The page reads its operations from the server.** `test_the_admin_page_keeps_no_operation_list_of_its_own` must
  keep passing: no `const TEMPLATES` or `const QUICK`, and the page still reads `tool_templates` and `quick_tools`.
- **Keep every state:** loading, empty, error and full on each screen. Keep unsaved-changes protection (the savebar, `Ctrl S`).
- **Neutral demo data:** `acme`, `example.com`, `*.test`.

## 1. Principles

- **Status first, settings second.** The first screen says what is broken and offers the fix next to it.
- **State in words, colour second.** "failing · 401 Unauthorized", not a red dot. Red at rest means a failure only.
- **One context.** Choose an environment once in the top bar. Every screen that takes an environment
  (Detect, Playground, Activity filter, Log fields scope) starts from that choice.
- **Everything links.** A call in Activity opens in the Playground. A failing environment opens its edit drawer.
  A search that found nothing links to "what graylog-mcp learned" and to Log fields.
- **Data is mono.** Queries, field names, instance keys (`acme/prod`), counts, durations and timestamps.
- **Keyboard first.** Every screen and action is in the command palette, and the common ones have a key.
- **Motion explains change.** Animate only what changed, using `transform` and `opacity`.

## 2. Shell and navigation

```
┌──────────────┬──────────────────────────────────────────────────────────────┐
│ ▣ graylog-mcp│ graylog-mcp / overview          [env: acme/prod ▾] [⌘K…] ⚙ ◐ │ top bar
│              ├──────────────────────────────────────────────────────────────┤
│ MONITOR      │                                                              │
│  Overview    │                       screen content                         │
│  Activity 12 │                                                              │
│ CONFIGURE    │                                                              │
│  Environments│                                                              │
│  Log fields  │                                                              │
│  Redaction   │                                                              │
│ TOOLS        │                                                              │
│  Playground  │                                                              │
│  Connect     │                                                              │
│              ├──────────────────────────────────────────────────────────────┤
│ , settings   │ ● server running · config ~/.config/…/config.toml · v0.3.1 ·  │ status bar
└──────────────┴──────────────────────────────────────────────────────────────┘
```

- **Sidebar.** Destinations are grouped under mono section labels:
  - **Monitor:** Overview, Activity
  - **Configure:** Environments, Log fields, Redaction
  - **Tools:** Playground, Connect

  "Config file" leaves the main nav. It opens from the status bar's config path, from the palette, and as a
  "Raw file" tab on any Configure screen.
  - Nav counts are mono and carry meaning: Activity shows the problem calls in range, and Environments shows how many are failing (in the error colour, with the word in the tooltip).
  - The active item has a 2px accent bar.
- **Top bar:**
  - a mono breadcrumb (`graylog-mcp / activity`)
  - the **environment picker**: a combobox of every instance grouped by group, with a health badge per row, plus "All environments"
  - the kbar (`Search or run a command… Ctrl K`)
  - Settings ⚙
  - a theme toggle

  The page title and its subline move into the screen body, as its first heading.
- **Status bar (new, 28px, sticky bottom):**
  - server state from `/api/setup`
  - the config path, which links to the raw file
  - the config status badge, moved here from the top bar
  - the version
  - an unsaved-changes notice

  The savebar keeps its own floating actions; the status bar only mirrors that changes are unsaved.
- **Environment context:**
  - Stored in `localStorage` (`gm-env`) and shown as a removable chip on each screen it filters.
  - Applies to: Activity (filter), Playground (env), Log fields (Detect env and "Apply to" scope), and Overview (highlights that row).
  - "All environments" clears it.
- **Breakpoints:**
  - ≤60rem: the sidebar becomes a top strip of icons, and the status bar hides everything but the server state.
  - ≤40rem: the environment picker becomes an icon button that opens a sheet.

## 3. Screens

Each screen lists its layout, what changed and the data it uses.

### 3.1 Overview (was Dashboard)

```
▸ acme · 4 environments · 3 healthy · 1 failing   ·   1,204 calls / 24h · 94% found something   [24h|7d] ⟳
┌ Needs attention ─────────────────────────────────────────────────────────────┐
│ ✕ acme/uat     failing · 401 Unauthorized               [Edit token]          │
│ ! 17 searches found nothing (top: service:checkout*)    [See in Activity]     │
│ ! Setup: Claude Code not registered                     [Register]            │
└──────────────────────────────────────────────────────────────────────────────┘
Environments (ledger, full width)                            KPIs (right rail)
 acme/prod   healthy  ▁▂▃▅▃▂  2.1M msgs · 312 err  210ms     calls      1,204 ↑8%
 acme/stage  healthy  ▁▁▂▁▁▁  …                                found      94%
 acme/uat    failing  401 Unauthorized             [Edit]      p95        640ms
Log volume (line) ─────────────── │ Errors per hour (bars) ─────────
Tools the model used (bars) ───── │ Searches that found nothing ────
```

- **New: a mono status line** at the top, built from totals in `/api/state`, `/api/doctor` and `/api/usage`.
- **New: Needs attention.** It merges failing environments, failed doctor checks, the setup steps still to do and
  search misses into one ranked list, each item with a single fix action. It replaces the separate setup card and
  "other checks". When the list is empty, it shows `// all clear`.
- **The ledger stays the signature.** Each row gains its message and error totals from `/api/logstats`, in mono.
- **KPIs move to a narrow right rail** of mono figures, each with its change against the previous period
  (`totals` and `previous` in `/api/usage`).

### 3.2 Activity

```
[24h|7d]  tool: [all ▾]  status: (all)(found)(nothing)(error)  env chip ✕  search…   ⟳
calls/hour (stacked: found / nothing / error) — click a bar to filter to that hour
┌ Calls │ By tool │ Learned ┐
 14:02:11  search_logs  acme/prod  service:"checkout" AND level:3   0 found  212ms  ▸
 14:01:58  aggregate    acme/prod  kind=top group_by=source          12        95ms  ▸
   └─ expanded: args · params · requests · returned/truncated · [Open in Playground] [Copy as JSON]
```

- **A log viewer, not a set of cards.** It is a dense mono list of `recent` calls, with column heads. A row shows
  time, tool, environment, what was asked, outcome badges (`✕ error`, `found nothing`, `fixed`, `cut`), duration and
  the result count. Only problems get a badge; a normal row stays quiet.
- **Layout (built in the prototype):** screen head with the range switch → the stacked **Calls** chart → tabs
  (Calls / By tool / Learned) → in Calls, the filter bar (search, tool, outcome) and the list. Overview and Activity
  share one range.
- **Filters run on the client,** over `recent`: free text over tool, environment, args and params; tool; outcome
  (all / found nothing / errors); the environment context; and the chart's hour. "Clear filters" resets all but the context.
- **The chart is a filter.** Clicking a bar limits the list to that bucket, and the hour shows as a removable chip.
  It is also keyboard-operable: focus the chart, then `←`/`→` move, `Enter` selects, `Esc` clears.
- **Two data sources, and the gap between them is real.** The chart and By tool come from the full `buckets` and
  `tools` of `/api/usage`; the list comes from `recent`, which keeps only the **latest 200 calls**. On a busy
  server those 200 cover a few hours, so a bar from two days ago may have no rows. The list then says
  `// no details kept for <hour> — the list keeps only the latest 200 calls` instead of looking broken, and the
  count line always reads "Showing N of M recorded calls · the latest 200 are kept".
- **The chart cannot narrow to one environment.** `summarize` has no instance filter, so under an environment
  context the chart keeps counting every environment (its meta line says so) and only the list narrows. See §10.
- **Tabs:**
  - **Calls:** the list.
  - **By tool:** calls, found nothing and errors from `tools` (all calls), with p50 and p95 in mono. A row is a link: it opens Calls filtered to that tool.
  - **Learned:** the `/api/learned` list (environment, fix, change, used / offered, weight bar, applied). **Forget** asks
    twice inline ("Sure?") instead of opening a confirm dialog.
- **A row expands in place** (grid-row collapse, not a re-render; rows are built once and filtering collapses them).
  It shows `args`, `params`, `requests`, `returned` of `results` and `truncated` (as "cut to the size budget"), and the
  call's error. Actions: **Open in Playground** (pre-fills the env, tool, and `args` merged with `params`), **Copy as
  JSON**, and, for a failed call on a failing environment, **Edit token** (the same fix dialog as Overview).
- **Keys:** `J`/`K` move between rows, `Enter` expands, and `P` opens the focused call in the Playground.
  They only act on the Calls tab. On touch devices the key hint is hidden.
- **Narrow screens:** the list becomes cards (tool and badges, what was asked, then time · environment · ms · count);
  the By tool table drops the bar and the latency columns.

### 3.3 Environments

```
[filter…]                                                        [+ Add environment]
┌ acme  "ERP"  default env: prod ─────────────────────────────── [⋯] ┐
│ ENV    STATE      URL                          AUTH     LATENCY      │
│ prod   healthy    https://graylog.example.com  token    210ms  [Edit]│
│ uat    failing    https://uat.example.test     token ✕  —      [Edit]│
│ REPOSITORIES  ~/code/acme-api  focus service:"acme-api"  [Focus][⋯] │
└──────────────────────────────────────────────────────────────────────┘
```

- Each group is a **ruled section**. Its head has the inline-editable description and the default environment.
  Under it are the env table, then the repositories as a sub-list.
- **Row click opens the drawer**, as today. The drawer becomes a native `<dialog>` with three tabs:
  - **Connection:** URL, auth and the advanced fields
  - **Check:** the last test result, with "Test now"
  - **Fields:** a shortcut to Log fields scoped to this environment
- **The test result shows inline in the row** after Test, with no toast for success.

### 3.4 Log fields

```
Scope:  global › acme › acme/prod        (breadcrumb, each level clickable; env context preselects)
┌ Fields (form) ──────────────────────────────┐ ┌ Detect from your logs ─────────┐
│ service field  [service        ] ← from acme│ │ env acme/prod · last 24h [Run]  │
│ level field    [               ] ← global:  │ │ service  98%  → suggested ✓     │
│                  level                      │ │ trace_id 61%                    │
│ error query    [level:<=3      ] set here ✕ │ │            [Apply suggestions]  │
└─────────────────────────────────────────────┘ └─────────────────────────────────┘
```

- **The scope becomes a breadcrumb.** It replaces the "Apply to" select and makes the inheritance chain visible.
- **Every field shows where its value comes from:**
  - `← from acme` (inherited, muted mono), or
  - `set here ✕`, where ✕ clears that level for this one field.
- **Detect moves to a sticky right column.** Applying suggestions highlights the fields that changed.

### 3.5 Redaction

- The two-column layout stays. The left column groups its sections under an accordion:
  - Profile
  - Packs
  - Custom patterns
  - Never mask
  - Field names
- **"Try it" becomes a before/after view.** The input sits above the output. In the output, every masked token
  (`[REDACTED]`, `[EMAIL]` and the rest) is highlighted with `--color-accent-soft`, and a hover shows which rule
  masked it, when `/api/redact` returns the rule. If it does not, see §10.
- The hit count is shown as mono text, for example `7 masked · 3 rules`.

### 3.6 Playground

```
┌ Request ─────────────────────────────┐ ┌ Result ──────────── 212ms · 4.1 KB · 3 masked ┐
│ quick: (errors 1h)(top sources)(…)   │ │ [Pretty | Raw JSON]                    [Copy]   │
│ env  [acme/prod ▾]  tool [search ▾]  │ │ { "messages": [ … ] }                           │
│ args (JSON, mono, line numbers)      │ │                                                 │
│ [Run  Ctrl ↵]                        │ └─────────────────────────────────────────────────┘
└──────────────────────────────────────┘ History (this session): 14:03 search_logs ✓ · 14:01 aggregate ✓
```

- **A split console:** the request on the left, the result on the right. They stack at ≤75rem.
- **The args editor** gets a line-number gutter, JSON validation on blur (inline error, Run disabled) and `Tab` indentation.
- **The result has two views:**
  - **Pretty:** collapsible JSON with mono keys.
  - **Raw:** the current `pre`.

  The meta line in mono shows the duration, the size and the masked count.
- **History of this session** (in memory, the last 20 runs) can be clicked to restore a run. It also receives the calls sent from Activity.

### 3.7 Connect (was Other clients)

- A tab per client (Claude Code, Claude Desktop, Cursor, VS Code). Each tab is a three-step stack:
  1. **How the server starts:** the existing source select.
  2. **The snippet:** a mono block with Copy, and the file path.
  3. **Install:** a one-click Install where `/api/clients/install` supports it.
- "Claude Code, every project" becomes the first tab's second card.

### 3.8 Config file (raw)

- It is reached from the status bar, the palette, and the "Raw file" tab on Configure screens.
- The editor has a line-number gutter. **Validate** runs on `Ctrl S` before saving, and an error marks its line
  with `--color-error-soft` when the validate response includes a line number.
- The rule against losing unsaved edits stays.

## 4. Colour schemes

There are four schemes, each with a light and a dark theme. Settings picks the scheme and the appearance:
**System / Light / Dark**.

- **Default:** Slate, with the appearance set to System. Slate light keeps today's values.
- **Attributes on `<html>`:** `data-palette="slate|midnight|graphite|catppuccin"`, `data-theme="light|dark"`
  (resolved) and `data-mode="system|light|dark"` (the user's choice).
- **Dark themes are softened.** Body text sits at about 11:1 against the background, not 14–15:1. Accents and status
  colours are less saturated. This came from prototype feedback that dark mode was too harsh, and every value
  still passes the checks below.
- **Every value below** is the same as the token blocks in `ui-demo/admin/admin-demo.html`. If the two ever
  disagree, the prototype wins.
- **Keep every existing token name.** JS template strings use `var(--chart-ink)` and similar, so a scheme only supplies values.

| Token | Role |
|---|---|
| `--color-paper` / `-2` / `-3` | Page and inputs / sidebar, wells, row hover / pressed, code, tracks |
| `--color-rule` / `-rule-2` | Hairlines / quieter dividers |
| `--color-ink` / `-ink-2` / `-muted` | Text / secondary text / meta and axes |
| `--color-field-edge` | Input borders (≥ 3:1 on paper) |
| `--color-accent` / `-hover` / `-ink` | Primary button, brand mark, links on hover |
| `--color-accent-soft` / `-soft-ink` | Active nav, ok badge, masked-token highlight, selection |
| `--color-warn*`, `--color-error*` | Pending and warning / failing, invalid, destructive |
| `--color-focus`, `--color-link-hover` | Equal to the accent |
| `--color-scrim`, `--shadow-card` | Overlays; floating surfaces only |
| `--chart-ink` / `-mid` / `-warn` / `-error-soft` | Found / secondary / found nothing / errors |

### A · Slate (default)

Tinted slate with teal for actions and data. Light keeps today's `index.html` values. Dark keeps today's paper,
`paper-2`, `paper-3`, scrim and shadow, and softens the rest:

| Token | Dark |
|---|---|
| rule / rule-2 | `oklch(40% 0.035 250)` / `oklch(33% 0.032 250)` |
| ink / ink-2 / muted | `oklch(88% 0.012 240)` / `oklch(80% 0.016 240)` / `oklch(72% 0.025 240)` |
| field-edge | `oklch(64% 0.04 245)` |
| accent / hover / ink | `oklch(73% 0.1 185)` / `oklch(79% 0.09 185)` / `oklch(22% 0.04 220)` |
| accent-soft / soft-ink | `oklch(37% 0.055 195)` / `oklch(85% 0.05 185)` |
| warn / soft / soft-ink | `oklch(78% 0.11 80)` / `oklch(40% 0.06 75)` / `oklch(88% 0.06 85)` |
| error / ink / soft / soft-ink | `oklch(71% 0.12 25)` / `oklch(22% 0.04 25)` / `oklch(37% 0.07 25)` / `oklch(85% 0.05 25)` |
| chart ink / mid / warn / error-soft | `oklch(73% 0.1 185)` / `oklch(62% 0.06 245)` / `oklch(78% 0.11 80)` / `oklch(58% 0.1 25)` |

### B · Midnight

A cool blue-black console. It is the opt-in near-black scheme; Slate dark stays charcoal, as the 0.3 changelog decided.

| Token | Light | Dark |
|---|---|---|
| paper / paper-2 / paper-3 | `#f7f8fa` / `#eff2f7` / `#e4e9f1` | `#12161e` / `#171c25` / `#1e2430` |
| rule / rule-2 | `#d5dbe5` / `#e6eaf0` | `#2b3341` / `#222833` |
| ink / ink-2 / muted | `#1a2030` / `#2d3648` / `#566175` | `#c3cbd7` / `#a9b2c0` / `#838d9e` |
| field-edge | `#7d889b` | `#66728a` |
| accent / hover / ink | `#3a64e0` / `#2f55c7` / `#ffffff` | `#6a8ad8` / `#829ee6` / `#0b0e14` |
| accent-soft / soft-ink | `#dfe7fd` / `#1f3a8a` | `#1f2a42` / `#b0c0e6` |
| warn / soft / soft-ink | `#9a6210` / `#fbedd0` / `#6b4308` | `#d6a24c` / `#372d18` / `#ead2a0` |
| error / ink / soft / soft-ink | `#c93a3a` / `#ffffff` / `#fbe2e2` / `#8f2424` | `#d96a6d` / `#1a0b0c` / `#3a2023` / `#eabfc0` |
| chart ink / mid / warn / error-soft | `#3a64e0` / `#7f8aa0` / `#e0a23a` / `#e58a8a` | `#6a8ad8` / `#838d9e` / `#d6a24c` / `#b25558` |
| scrim | `rgb(26 32 48 / .32)` | `rgb(4 6 10 / .6)` |
| shadow-card | `0 20px 40px rgb(26 32 48 / .16)` | `0 0 0 1px #2a3240, 0 24px 60px -12px rgb(0 0 0 / .65)` |

### C · Graphite

Neutral zinc greys with a violet accent. The dark theme is charcoal, not black.

| Token | Light | Dark |
|---|---|---|
| paper / paper-2 / paper-3 | `#fafafa` / `#f4f4f5` / `#e9e9ec` | `#18181b` / `#1f1f23` / `#2a2a30` |
| rule / rule-2 | `#e0e0e4` / `#ececef` | `#34343b` / `#28282e` |
| ink / ink-2 / muted | `#18181b` / `#2e2e33` / `#5f5f68` | `#d1d1d6` / `#b6b6bd` / `#94949d` |
| field-edge | `#8b8b94` | `#707079` |
| accent / hover / ink | `#6e56cf` / `#5e47b8` / `#ffffff` | `#9483dc` / `#a898e6` / `#16121f` |
| accent-soft / soft-ink | `#ece8fb` / `#45309a` | `#302a48` / `#cbc3ec` |
| warn / soft / soft-ink | `#9a5c06` / `#fdf1c7` / `#6b4105` | `#d8b13e` / `#38311a` / `#ecdca0` |
| error / ink / soft / soft-ink | `#c42727` / `#ffffff` / `#fde4e4` / `#8c1d1d` | `#dd6a6a` / `#1c0a0a` / `#3e2222` / `#eac0c0` |
| chart ink / mid / warn / error-soft | `#6e56cf` / `#8b8b94` / `#d9a21b` / `#ec8b8b` | `#9483dc` / `#85858e` / `#d8b13e` / `#b05a5a` |
| scrim | `rgb(24 24 27 / .32)` | `rgb(9 9 11 / .55)` |
| shadow-card | `0 20px 40px rgb(24 24 27 / .16)` | `0 0 0 1px #34343b` |

### D · Catppuccin

The community Catppuccin palette (MIT licensed, catppuccin.com): **Latte** for light and **Mocha** for dark, with
mauve as the accent.

| Role | Latte | Mocha |
|---|---|---|
| paper / paper-2 / paper-3 | base `#eff1f5` / mantle `#e6e9ef` / crust `#dce0e8` | base `#1e1e2e` / mantle `#181825` / surface0 `#313244` |
| ink / ink-2 / muted | text `#4c4f69` / `#55586f` / subtext1 `#5c5f77` | text `#cdd6f4` / subtext1 `#bac2de` / `#9a9fb8` |
| field-edge | overlay2 `#7c7f93` | overlay0 `#6c7086` |
| accent | mauve `#8839ef` | mauve `#cba6f7` |
| warn / error | `#985a0f` / red `#d20f39` | yellow `#f9e2af` / red `#f38ba8` |
| chart warn / error-soft | yellow `#df8e1d` / `#ee7f97` | peach `#fab387` / maroon `#eba0ac` |

Four values are darkened from stock Latte, and one lightened from stock Mocha, to pass contrast: Latte ink-2,
muted, field edge and warn text, and Mocha muted.

Latte ink-2 is about 6.2:1, below the 7:1 target for secondary text but well above the 4.5:1 AA minimum. This keeps
the palette recognisable.

### Contrast (checked)

All eight scheme/mode pairs pass these checks:

- ink on paper: ≥ 7:1, and about 11:1 in dark themes; ink-2 ≥ 7:1, except Latte at about 6.2:1
- muted on paper, paper-2 and paper-3: ≥ 4.5:1
- the `-ink` text on each fill: ≥ 4.5:1
- error and warn as text: ≥ 4.5:1
- accent, chart-ink and field-edge on paper: ≥ 3:1

Known gap, as today: the `chart-warn` and `chart-error-soft` bars are below 3:1 on light paper. They are always
paired with a legend, a tooltip and numbers.

Tints use `color-mix(in oklab, var(--x) N%, transparent)`, never alpha hexes.

## 5. Typography

```css
--font-ui:   ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
--font-mono: ui-monospace, "SF Mono", "Cascadia Code", "JetBrains Mono", Menlo, Consolas, monospace;
```

- Alias `--font-outlier`, `--font-display` and `--font-body` to these for one release.
- **Base:** 14px / 1.5. Screen h1: `--text-2xl` / 600. Section h2: `--text-lg` / 600.
- **Section labels:** sidebar groups, table heads and KPI labels are 11px mono, uppercase, with 0.08em letter spacing.
- **Dense lists:** Activity rows and the ledger are 13px mono with `tabular-nums`. Row height is 32px, or 44px for coarse pointers.
- **KPI figures:** mono, 28–33px, -0.04em letter spacing.

## 6. Layout and shape

- **Sidebar:** 224px. The status bar is 28px.
- **Width cap per screen:**
  - 1520px: Overview and Activity
  - 1320px: Environments, Playground and Log fields
  - 1120px: the others
- **Radii:** `--radius-control: 8px` and `--radius-card: 12px`. Badges are pills.
- **Rules, not boxes.** A section opens on a 1px `--color-ink` rule. Fills (`.well`) are kept for forms.
  Shadows are for floating surfaces only.

## 7. Components

| Component | Spec |
|---|---|
| **Environment picker** (new) | A combobox (`role=combobox` plus a listbox) grouped by group. Each row shows `acme/prod`, then its health word, then the latency. Typing filters the list. "All environments" is first. |
| **Needs attention** (new) | A ruled list. Each row is a glyph (✕ or !), a mono subject, a reason in words and one action button. The order is failing environments, then doctor failures, then setup steps, then misses. |
| **Status line** (new) | 13px mono with a `▸` prompt in the accent colour and `·` separators. |
| **Log list** (new) | Activity rows: mono columns for time, tool, env, args, result, ms and a chevron. A row can expand. `J`/`K` move the selection, and the selected row has a 2px inset accent bar. |
| **Scope breadcrumb** (new) | A mono `global › acme › acme/prod` path. Each level is a button. The current level is ink, the others muted. |
| **Provenance tag** (new) | After a field: `← from acme` (muted mono) or `set here ✕`. |
| **JSON view** (new) | Collapsible keys. Keys use `--color-ink-2`, strings `--color-accent-soft-ink`, numbers `--chart-mid`. Mono, 12px. |
| **Status bar** (new) | 28px, mono 12px, `--color-paper-2`, with a top hairline. |
| **Settings dialog** (new) | Native `<dialog>`: three scheme cards with a split light/dark preview; Appearance segments; Density (Comfortable / Compact, which changes row heights only); a shortcut list. |
| **Segmented control** | A sliding pill (`--color-paper` with a hairline). The accent is kept for primary actions. |
| **Drawer** | A native `<dialog>` with tabs, and enter and exit transitions. |
| **Buttons** | Press is `scale(.97)` (`.92` for icon buttons). The primary button has a hover ring and an inline kbd. |
| **Empty states** | Mono and muted, for example `// no calls in the last 24 hours — the model has not used graylog-mcp yet`, with one action. |

## 8. Motion and keyboard

**Motion tokens:**

```css
--ease-out: cubic-bezier(0.16, 1, 0.3, 1);
--dur-micro: 120ms;
--dur-short: 220ms;
--dur-long: 320ms;
```

| Interaction | Behaviour |
|---|---|
| Segment and nav indicator | One pill moves with `translateX` and `width`. It is re-measured when its screen becomes active. |
| Screen switch | The incoming screen fades in and rises 4px. |
| Row expand | `grid-template-rows: 0fr → 1fr` with opacity. |
| Needs-attention item resolved | The row collapses after its fix succeeds. |
| Dialogs and drawer | `@starting-style` and `allow-discrete`, for both enter and exit. |
| Theme change | A View Transition circular reveal (520ms). A scheme change crossfades (300ms). |
| KPIs and bars | Figures count up over 480ms on the first load of a range. Bar fills use `scaleX`. |

Under reduced motion, durations become 0.01ms and View Transitions are skipped.

**As built (Motion).** `ui/src/scripts/motion.ts` is the one place script animation lives, on the full `motion` package
(about 75KB raw added to the page, which is fine for a page served from the local machine). CSS keeps hover, press, the row expand and the dialogs; Motion does what needs
a spring, a per-element delay or a finish callback:

| Interaction | Behaviour |
|---|---|
| Screen switch | The blocks in view rise 10px and fade in, 45ms apart (the whole run is capped at ~300ms). Inline styles are removed when each finishes, so sticky children keep working. |
| Confirm box, palette | The scrim fades and the panel springs in from 10px up at 97%. They leave in 140ms, no bounce. |
| Toasts | Slide in from the right on a spring. On leaving they slide out and the gap closes, so the toasts below do not jump. |
| Pill | A spring (0.38s, bounce 0.12) between the pressed buttons. |
| Scheme card | A small spring pop when chosen. |
| Figures | The Overview figures count up with `animate(0, value)` once per range. |
| Activity list | The first rows in view rise in 30ms apart when the calls load. |
| Overview charts | The line is traced, the area fades in, bars grow from the axis, horizontal bars grow from the left. Once per data set: a render with the same range, environment, width and values leaves the chart alone. |

Reduced motion skips all of it (`REDUCED` in `motion.ts`, re-exported by `core.ts`).

**Keyboard.** The existing keys stay: `Ctrl/⌘ K`, `Ctrl/⌘ S`, `Ctrl/⌘ Enter` and `Esc`.

| Key | Action |
|---|---|
| `/` | Command palette |
| `E` | Environment picker |
| `,` | Settings |
| `T` | Light/dark |
| `1` `2` `3` `4` | Slate / Midnight / Graphite / Catppuccin |
| `G` then `O` `A` `E` `F` `R` `P` `C` | Go to Overview, Activity, Environments, Fields, Redaction, Playground, Connect |
| `J` / `K`, `Enter`, `P` | Activity: move the selection, expand the row, open the call in the Playground |

Single keys are ignored while typing or while an overlay is open.

## 9. Accessibility and persistence

- **Native `<dialog>`** for Settings and the drawer.
- **Roles:** the combobox and listbox pattern for the environment picker and the palette; `aria-expanded` on Activity rows; `aria-pressed` on segments and status chips.
- **Focus** is visible in all six themes, and it returns to the opener when an overlay closes.
- **No colour-only status.** Glyph and word, always.

**Storage** (all reads in try/catch, validated, applied before first paint where they affect the look):

| Key | Values | Default |
|---|---|---|
| `gm-theme` | `light` \| `dark` \| absent (System) | absent. Existing key, kept |
| `gm-palette` | `slate` \| `midnight` \| `graphite` \| `catppuccin` | `slate` |
| `gm-density` | `comfortable` \| `compact` | `comfortable` |
| `gm-env` | an instance key, or absent | absent (All) |

If `gm-env` names an instance that no longer exists, it is dropped silently.

## 10. Needs the server (optional follow-ups, separate PRs)

The revamp works without these. Each one would improve a screen.

- **`/api/redact` returns each match's rule name and span.** This would enable the "which rule masked it" hover.
  The fallback highlights tokens by regex on the client.
- **`/api/config/validate` returns a line number.** This would mark the error line in the raw editor.
  The fallback shows the message only.
- **`/api/usage` accepts an instance filter (and `recent` is longer or paged).** This would let the Activity chart
  follow the environment context and let an old bar show its calls. The fallback is described in §3.2.
- **Server state in `/api/state`.** This would save a request for the status bar. The fallback is `/api/setup`.

## 11. As built (v3 in `src/graylog_mcp/admin/static/index.html`)

**Preact migration (2026-11-18).** The page is built with Vite, Preact and `@preact/signals`; shared state,
environment context, range and routing live in `ui/src/store.ts`. Each screen and overlay is JSX with real
handlers. SVG charts and the level donut are JSX; data-driven dimensions and colours use CSSOM styles. Native
dialogs follow signals through refs, and Motion remains in `scripts/motion.ts` with reduced-motion handling.
The Overview breakdown and logs drawer are preserved. The global CSS, tokens and class names remain in use.
Astro, HTML-string renderers, event delegation, DOM lookup typing and the style MutationObserver are removed.
The palette and confirmation use native dialogs for keyboard focus containment and focus restoration; their
panel geometry and Motion animations stay the same. The Python API is unchanged; static files use `assets/`.
Chart tooltips render through a Preact portal so screen transforms cannot offset them. Tooltip and picker
positions use explicit pixel units, as required by Preact 11. Screen entrances wait one frame for sibling
signal subscribers to finish hiding the previous screen before measuring visible blocks.


Where the shipped page differs from the sections above. The shipped page wins.

- **Environments.** Each environment opens a `<dialog>` with three tabs: Connection, Check and Fields. Check shows the last
  Test result only when there is one, so a failure is not listed twice. Local rows have a Test button. There is no latency
  column, because `/api/doctor` and `/api/test` report none.
- **Log fields.** The scope breadcrumb shows the chain of the level being edited. The old select stays beside it as
  "Other level" so any group or environment can be picked. Each field's provenance tag ("from X", or "set here ✕") is worked out
  in the page from `/api/scope` of each ancestor. Detect is a sticky right column and defaults to the context environment.
- **Redaction.** The masked count is the number of redactor marks in the output, counted in the page. A mark does not say which rule made it.
- **Playground.** Run is not disabled on invalid JSON. It refuses with a message under the editor. History keeps 20 runs in memory.
- **Connect.** Claude Code is the first tab and carries the "every project" command.
- **Config.** A "Raw file" button sits in the head of Environments, Log fields and Redaction. The error line is marked only
  when the message names one.
- **Overview and Activity.** The tool bars and the chart do not narrow to the context environment (no instance filter in
  `/api/usage`); the page says "all envs".
- **Motion.** Preact refs and effects call `scripts/motion.ts`: visible screen blocks rise 10px and fade in, with a capped stagger; the initial screen is still. Overview figures count up over 600ms once per range and figure; a refresh of the same range sets them at once. Reduced motion skips both.
- **Errors.** A failed `/api/usage` shows its message in Activity and in the Overview "found nothing" list. A failed `/api/doctor`
  shows "check failed" in the status line and "not checked" on each environment. Each also raises a toast.
- **Leaving with unsaved edits.** The confirm dialog opens on Cancel when the action is destructive.
- **Separators.** The `·` and `│` glyphs use the muted text colour and are `aria-hidden`, so they meet text contrast.
- **Against the Phase 0 prototype (Slate dark, Overview and Activity).** The layout matches. Differences: no p95 tile and no latency
  column; KPI labels are sentence case, not small caps; health badges have no ✓/✕ glyph; Settings is the gear in the top bar, not a
  link in the sidebar foot; the Calls line omits "every environment".
- **Load failures.** Config shows "Could not read the config file" and goes read-only, so an empty box cannot be saved over the
  file. Log fields shows "Could not load these settings". Connect shows "Could not load the client snippets". While loading, Log fields shows "Loading…" and the Config editor is read-only.
- **Shell against the prototype (measured on all 8 real screens, all 8 scheme x theme combinations).** The shell is the same on every screen.
  The sidebar (224x943, same background and borders) and the status bar (1441x28, same colours and mono 12px) match the prototype
  exactly in all 8 themes. The active nav item is 199x32 in both, with the same background, text colour, 2px accent bar, font and size. The shell was read with transitions switched off (an earlier read
  80ms after a theme switch caught the prototype mid-transition and wrongly looked like a mismatch): for each of the 8 themes, each of
  the 8 real screens gave one identical sidebar, status bar and active-item record (7 screens have an active item; Config has none), and
  it equals the prototype's record string for string. No script error was raised on any of the 64 screen runs, Redaction included. The `--color-accent-soft` and `-soft-ink` tokens are identical in the two files. Differences: the top bar is 55px, not 57px, and opaque, not translucent with a blur; the controls are 38px high, not 36px
  (`--control`); the search button is 240px wide, not 274px; the Config screen has no active nav item because it is reached from the status bar.
  Result per combination (sidebar, status bar and active item; Config has no active item):

  | Scheme / theme | Sidebar | Status bar | Active item | Result across 8 screens |
  |---|---|---|---|---|
  | Slate / light | match | match | match | pass |
  | Slate / dark | match | match | match | pass |
  | Midnight / light | match | match | match | pass |
  | Midnight / dark | match | match | match | pass |
  | Graphite / light | match | match | match | pass |
  | Graphite / dark | match | match | match | pass |
  | Catppuccin / light | match | match | match | pass |
  | Catppuccin / dark | match | match | match | pass |

- **Layout.** All screens share one left edge, 40px from the sidebar, and one maximum width (1520px), so the breadcrumb aligns with the content and the content box never changes between screens. It is left-aligned, not centred, so on wide windows the empty space is on the right.
