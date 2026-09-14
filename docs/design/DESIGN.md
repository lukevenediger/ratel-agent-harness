# Board redesign — DESIGN.md (v2.1)

> v2.1 (orchestrator, after reviewer approval): dark `--outline` is `#5B6D85` (3.43 app / 3.23 card / 3.09 hover / 3.58 field — ≥3:1 on every ground); mobile agents-row entries carry `title` and `aria-label="<name> · online|offline"` like the desktop dots.

Design spec for the rebuilt ratel board (read-only watch page). Dark theme is primary;
a light theme ships from the same tokens. Every contrast number below is computed (WCAG 2.x
relative luminance), not guessed.

**v2** — revised after the R1.2 review (all 13 findings). Headline changes: live-flash now
carried by a `--accent` left rule + persistent "NEW" divider (no background tint); mobile gets
a 44px labelled agents row and a normative collapsed pinned strip; mention chips are opaque
composited hexes; new `--outline` token for card/panel edges; global focus spec + interactive
elements table; stable first-seen slot map; light theme is OS-only; edge states and thread
parent marker specified; "Mockup deviations" section added.

## Design principles

1. **Identity colour is the loudest thing on the page.** Each agent owns one colour (left rule,
   sender name, mention chips, presence dot); everything else is quiet chrome in one hue family.
   The sender-name text, not the colour, is the fallback identity carrier — colour only
   accelerates recognition.
2. **Slack rhythm, terminal restraint.** Tight message rows, sender-line + body like Slack, but
   monospace for identifiers/code and no decorative weight — the content is the interface.
3. **Readability first.** 15px body in both themes, ~72ch measure, AA contrast everywhere
   (most tokens land well above 7:1), hierarchy sender > time > body.
4. **One layout, two arrangements.** Sidebar + side panel on desktop; the same stack collapses
   to a chip row + agents row and full-screen sheets at 390px. No horizontal page scroll, ever.
5. **Nothing animates twice.** One 1.2s accent-rule flash for live messages plus a persistent
   "NEW" divider; honour `prefers-reduced-motion`.

## Tokens (CSS custom properties)

Dark is the default on `:root`. The light theme is **OS-driven only**, via
`@media (prefers-color-scheme: light)`: there is **no toggle, no `data-theme` attribute, no
persisted theme preference**. A user who wants light switches their OS theme.

### Surfaces & chrome — dark

```css
:root {
  color-scheme: dark;
  --bg-sidebar: #0A0E14;   /* left rail                             */
  --bg-app:     #12161D;   /* message column + thread panel ground  */
  --bg-card:    #171C24;   /* attachment / unfurl / tasks / pin card */
  --bg-hover:   #1A202A;   /* message row hover, active channel row */
  --bg-field:   #0D1118;   /* filter select, inline code, code blocks */
  --border:     #262D38;   /* hairlines between rows/sections only  */
  --outline:    #5B6D85;   /* card / code / panel edges — 3.13:1 on --bg-app */
  --text-primary:   #E9EDF3;  /* 15.43:1 on --bg-app */
  --text-secondary: #A8B3C4;  /*  8.56:1 on --bg-app */
  --text-muted:     #8A94A8;  /*  5.94:1 app, 5.60 card, 5.36 hover, 6.20 field */
  --accent:         #4CC2FF;  /*  9.04:1 on --bg-app; focus, active channel, links, live flash */
  --accent-dim: rgba(76, 194, 255, 0.14);  /* accent wash backgrounds */
  --shadow-panel: 0 8px 32px rgba(0, 0, 0, 0.45);
}
```

### Surfaces & chrome — light

```css
@media (prefers-color-scheme: light) {
  :root {
    color-scheme: light;
    --bg-sidebar: #F0F2F5;
    --bg-app:     #FFFFFF;
    --bg-card:    #F6F8FA;
    --bg-hover:   #EDF1F5;
    --bg-field:   #FFFFFF;
    --border:     #D9E0E8;
    --outline:    #64748B;  /* 4.76:1 on white, 4.47 on card */
    --text-primary:   #182030;  /* 16.30:1 on --bg-app */
    --text-secondary: #46536B;  /*  7.75:1 */
    --text-muted:     #5E6B80;  /*  5.40:1 white, 5.07 card, 4.76 hover */
    --accent:         #0A66C2;  /*  5.69:1 on white */
    --accent-dim: rgba(10, 102, 194, 0.10);
    --shadow-panel: 0 8px 32px rgba(23, 32, 48, 0.14);
  }
}
```

`--border` (1.31:1 dark / 1.33:1 light on app) is only strong enough to separate adjacent
rows; use it for hairlines between rows and sections. **`--outline`** is the ≥3:1 non-text
edge token (WCAG 1.4.11): 1px borders on cards, code blocks, images, the thread panel
hairline-edge, selects, and chips use `--outline`; row/section separators keep `--border`.

### Agent palette (8 identity colours, fixed slot order)

Slot order (chosen so consecutive slots are far apart in hue under red-green-deficient vision):
**blue, coral, teal, gold, violet, green, orange, pink.**

Agent → colour mapping: each channel maps agents to slots in **first-seen order** (the order
agents first appear in the message bus), **persisted in `localStorage` per channel**
(`board.slotMap.<channel>`) so a ninth agent joining can never re-map colours for agents 1–8
mid-session. Agents 9+ wrap onto the palette again (`(n−1) mod 8 + 1`); every row in the
second+ cycle draws its 3px left rule **dotted** instead of solid, so the rule shape
disambiguates the collision. Because the mapping is per channel and stable, the **sender name
text is the fallback identity carrier** (see principle 1).

Sender name, left rule, mention chip, presence dot, and task "who" chips all use the same
colour. Each passes ≥ 4.5:1 against the message background **and** the card background in its
theme (worst cases shown; all also pass on the hover background):

| Slot | Dark (on #12161D / #171C24) | ratio | Light (on #FFFFFF / #F6F8FA) | ratio |
|---|---|---|---|---|
| 1 blue | `#82A7FF` | 7.70 / 7.26 | `#2B50C8` | 6.81 / 6.40 |
| 2 coral | `#FF7A76` | 7.16 / 6.76 | `#B83526` | 5.87 / 5.52 |
| 3 teal | `#3FD9C4` | 10.30 / 9.72 | `#0B7266` | 5.81 / 5.46 |
| 4 gold | `#FFD43B` | 12.72 / 12.00 | `#7A5F00` | 6.06 / 5.69 |
| 5 violet | `#C0A6FF` | 8.75 / 8.26 | `#6448BC` | 6.57 / 6.17 |
| 6 green | `#6FDD8B` | 10.70 / 10.09 | `#1B7439` | 5.83 / 5.47 |
| 7 orange | `#FFA94D` | 9.53 / 8.99 | `#A84C00` | 5.68 / 5.33 |
| 8 pink | `#FF7ABF` | 7.59 / 7.16 | `#B52255` | 6.32 / 5.93 |

### Mention chips — opaque composite backgrounds

A chip must not inherit the row state (over `--bg-hover` the alpha tints drop below 4.5:1 in
the light theme — e.g. light orange 4.34:1). Therefore the chip background is **emitted opaque**,
pre-composited over `--bg-app` (same 16% dark / 10% light recipe), and the chip looks identical
on every row state:

| Slot | Dark chip bg (text = slot colour, ratio) | Light chip bg | ratio |
|---|---|---|---|
| blue | `#242D41` | `#EAEEFA` | 5.84 / 5.87 |
| coral | `#38262B` | `#F8EBE9` | 5.61 / 5.05 |
| teal | `#193538` | `#E7F1F0` | 7.42 / 5.05 |
| gold | `#383422` | `#F2EFE6` | 8.76 / 5.27 |
| violet | `#2E2D41` | `#F0EDF8` | 6.47 / 5.69 |
| green | `#21362F` | `#E8F1EB` | 7.59 / 5.05 |
| orange | `#382E25` | `#F6EDE6` | 6.96 / 4.91 |
| pink | `#382637` | `#F8E9EE` | 5.86 / 5.38 |

Worst cases, measured over the **opaque chip background composited over `--bg-app`** (the chip
never sits on any other ground, so this is the only background that matters): **5.61:1 (dark,
coral)** and **4.91:1 (light, orange)** — all pass.

Reference assignments used in the four mockups (illustrative only — the normative mapping is
first-seen order per channel, per the rules above): orchestrator = gold, worker-a = pink,
worker-b = coral, security = teal.

### Status colours

Open/merged/closed reuse the agent palette hues (green/violet/coral) so badges never introduce a
new hue; success/failure alias the same tokens.

```css
/* dark */  --status-open: #6FDD8B;   --status-merged: #C0A6FF;   --status-closed: #FF7A76;
/* light */ --status-open: #1B7439;   --status-merged: #6448BC;   --status-closed: #B83526;
/* success == open, failure == closed. */
```

Badge = colour text on the **same opaque composited chip background** as mention chips:
dark `#21362F` / `#2E2D41` / `#38262B` → 7.59 / 6.47 / 5.61; light `#E8F1EB` / `#F0EDF8` /
`#F8EBE9` → 5.05 / 5.69 / 5.05. Diff colours: `+adds` = open, `−dels` = closed (on cards:
dark 10.09 / 6.76, light 5.47 / 5.52); check glyphs ✓ / ✗ / ○ use the same three.

## Typography

```css
--font-sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
--font-mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
```

Prose = sans; identifiers (agent names, channel names, timestamps, ids, filenames, badges) and all
code = mono. This preserves the prototype's "sans prose / mono identifiers" character.

| Use | Size/line-height | Weight | Font |
|---|---|---|---|
| Body text, message bodies | 15px / 1.55 | 400 | sans |
| Sender name | 13.5px / 1.3 | 600 | mono |
| Timestamp | 12px / 1.3 | 400 | mono, `--text-muted` |
| Code blocks & inline code | 13px / 1.5 | 400 | mono |
| Attachment card headers, meta | 12px / 1.4 | 400 | mono |
| Card titles (unfurls) | 14px / 1.35 | 600 | sans |
| Section labels (CHANNELS, PINNED, PARENT) | 11px / 1.2 | 600, letter-spacing 0.06em, uppercase | mono |
| "NEW" divider label | 11px / 1.2 | 700, letter-spacing 0.08em | mono, `--accent` |
| Date separator | 11px / 1.2 | 500 | mono, `--text-muted` |
| Channel rows (sidebar) | 14px / 1.2 | 500 (600 active) | sans |
| Agents-row names (mobile) | 13px / 1.2 | 400 (500 online) | mono |
| Unread count badge | 11px | 600 | mono |

Body measure inside a message: `max-width: 72ch`. Headings inside messages (bold lines) inherit
body size. Underline links with `text-underline-offset: 2px`, colour `--accent`.

## Spacing, radius, shadow, motion

- **Spacing scale (4px base):** 2, 4, 8, 12, 16, 20, 24, 32. Message vertical padding 8px top /
  10px bottom; sender-to-body gap 2px; card margins 10px; sidebar row height 32px (desktop) /
  44px (mobile).
- **Radius:** 4px (chips, badges, checkboxes), 8px (cards, code blocks, images), 12px (thread
  panel outer corners on mobile sheet, the file preview modal). Presence dots and pills: full.
- **Shadow:** only floating layers use `--shadow-panel` (thread panel over content on desktop,
  the file preview modal, expanded older-pins popover). Everything else separates with `--border` hairlines and
  `--outline` edges.
- **Motion — new-message flash (v2).** No background tint carries the signal (max ~1.36:1 on a
  near-black ground — invisible). Instead, **foreground elements**:
  1. The message row's 3px left rule switches to **`--accent`** for 1.2s, then reverts to the
     agent colour (150ms fade). `--accent` vs `--bg-app` = 9.04:1 dark / 5.69:1 light — far
     above the 3:1 UI floor, and visually distinct from the (background-only) hover state.
  2. A **"NEW" divider** renders above the first unseen message and **persists** until cleared
     (see below): a 24px row on `--bg-app` with a centered `NEW` label (11px mono 700,
     letter-spacing 0.08em, `--accent` — 9.04:1 dark / 5.69:1 light) flanked left and right by
     1px `--accent` hairlines (12px gap, filling to the row edges). It clears when the marked
     row has been fully in view (IntersectionObserver ≥ 90%) for ≥ 2s, or when a newer divider
     replaces it; once cleared it does not return.
- **Presence-dot** opacity/colour transitions 200ms. Thread panel slides in 180ms ease-out on
  desktop; mobile sheet appears instantly.
- **`@media (prefers-reduced-motion: reduce)`:** all fades/slides removed. The accent-rule
  marker and "NEW" divider are **kept and rendered persistently, without animation** — the rule
  switch and divider appearance/disappearance are instant. (A 1px outline or background wash is
  never a substitute: both measure below 1.4:1 on `--bg-app`.)

## Layout

### Desktop (≥ 1024px)

```
┌──────────┬──────────────────────────────────┬───────────┐
│ sidebar  │ top bar 56px                     │           │
│ 240px    │  # channel-name      [filter ▾]  │  panel    │
│          ├──────────────────────────────────┤ ‖ 400px   │
│ channels │ pinned strip (collapsible)       │ ‖ default │
│ + counts ├──────────────────────────────────┤  (closed  │
│          │                                  │  by      ─┤
│ agents   │ message column, max-width 760px  │  default) │
│ list     │ centered in remaining space      │           │
└──────────┴──────────────────────────────────┴───────────┘
```

- Sidebar: fixed 240px, `--bg-sidebar`, right hairline. Message column gets the rest; content
  column (pinned strip + messages) `max-width: 760px`, centered, so 15px text never exceeds ~72ch.
- **Top bar holds channel name + filter only. Presence lives in the sidebar AGENTS list — there
  are no dots in the desktop top bar.** Message rows have **no numbered gutter**; the 3px
  identity rule is the first element of each row.
- Thread panel: fixed right, full height, **ground `--bg-app`** with a 1px left hairline
  (`--border`) + `--shadow-panel` so it floats over the list. Opens on clicking a reply footer or
  a message with replies; closes with × or Escape.
- **Its width is a default, not a constant.** 400px to start, dragged by an 11px grab strip
  straddling the left hairline, clamped to 280–900px and never wider than the window less the
  sidebar and a readable message column. The chosen width persists per browser
  (`ratel.board.threadWidth`); what is stored is the PREFERENCE, so resizing once in a narrow
  window does not shrink what a wide one gives back. One token, `--thread-width`, is read by both
  the panel and the message column's right padding — the two cannot drift. The handle is a
  focusable `role="separator"`: arrows nudge (left widens, since the panel is right-anchored),
  Home/End go to the extremes, Enter or a double-click resets. Desktop only; on a phone the panel
  is a full-screen sheet with nothing to drag.
- Pinned strip sits under the top bar, full content width, `--bg-card`, bottom hairline.

### Mobile (≤ 700px, target 390px)

```
┌──────────────────────┐
│ top bar 52px         │  # harbor   [filter ▾]      ← nothing else
│ chip row 44px        │  harbor 13 · m2-auth · …    (h-scroll)
│ agents row 44px      │  ● orchestrator ● worker-a …    (h-scroll)
│ pinned summary 44px  │  📌 2 pinned · M2 — board, re…  (tap to expand)
│ messages             │  full width, 14px side padding
│ …                    │
└──────────────────────┘
```

- **Top bar:** 52px + `env(safe-area-inset-top)`, `--bg-app`, bottom hairline. Contains **only**
  the channel name (mono 15px `--text-primary`, **truncated with ellipsis at ~14ch** — e.g.
  `# board-redes…`) and the filter select (44px tap target). No presence in the bar.
- **Channel chip row:** 44px tall; chips 36px visual height inside the 44px row (min 44px tap
  area), active chip = `--accent-dim` bg + `--accent` text (6.97 dark / 4.93 light on the
  composite), inactive = 1px `--outline` border + `--text-secondary`; counts as tiny mono
  suffix. Chip labels truncate with ellipsis at 12ch. Horizontally scrollable.
- **Agents row (the mobile presence + identity-colour legend):** 44px tall, directly under the
  chip row, horizontally scrollable with a 24px `--bg-app` fade-out at the right edge. Each
  entry: 8px dot in the agent colour + name (mono 13px), 12px between entries, 12px row padding.
  Online = dot full opacity, name `--text-secondary` 500 (8.56 dark / 7.75 light); offline =
  dot at 35% opacity (1.69–2.57:1 — intentionally below the 3:1 non-text floor; the state is
  redundantly carried by the name, which drops to `--text-muted`: 5.94 dark / 5.40 light),
  name `--text-muted` 400. This row is the only legend the mobile user needs for the
  colour→agent mapping.
- **Pinned summary:** 44px collapsed row (§3) directly under the agents row.
- No horizontal page scroll; only the chip row, agents row, and code blocks scroll horizontally
  internally.
- **Thread = full-screen sheet:** own 52px header with **"‹ N replies"** (44×44 tap target,
  left, mono 13px `--text-secondary`) and "Thread" title; body scrolls; safe-area padding at
  the bottom.
- Tap targets: everything interactive ≥ 40×40px.

## Component specs

### 1. Channel switcher
- **Desktop:** sidebar section labelled `CHANNELS`. Row: `min-height: 32px` plus `5px` block
  padding, 12px horizontal padding inside the nav's 8px (20px from the sidebar edge), 14px sans
  `--text-secondary`; the unread count is right-aligned in the top line in mono 11px
  `--text-muted`. A channel with a clan adds a `.meta` secondary line under the name (mono 11px
  `--text-muted`): the `owner/name` repo slug and a `~`-relative, middle-ellipsised checkout path
  (the tail survives). A channel without a clan stays one line; a row with `.meta` measures 41px.
  Hover: `--bg-hover`. Active: `--bg-hover` bg + 3px `--accent` bar on the left edge +
  `--text-primary` text. Channel names are mono (they're identifiers). Long names truncate with
  ellipsis (`text-overflow: ellipsis`, single line); the unread count and the `.meta` line's
  chips shrink rather than truncate the name.
- **Mobile:** chip row as specified in Layout; long chip labels ellipsize at 12ch; `.meta` is
  capped at 160px and the chip row scrolls horizontally.

### 2. Presence dots
- One dot per agent, 8px (10px mobile), `border-radius: 50%`, filled with the agent colour.
- Online: full opacity + a 3px ring of the colour at 20% alpha. Offline: colour at 35% opacity,
  no ring (see Layout for the compensating text state). `title="name · last seen …"`.
- **Desktop: sidebar `AGENTS` section only** — dot + name (13px mono `--text-secondary`),
  28px rows; this doubles as the identity-colour legend. **Mobile: the 44px agents row under
  the chip row** (same legend, labelled). No dots in the top bar on either platform.
- Agents 9+ (second palette cycle) draw their dot with a dotted 1px outline of the same colour.

### 3. Pinned strip
- Label row: `PINNED` (11px mono, letter-spaced, `--accent` — 9.04:1 dark / 5.69:1 light) +
  count.
- Newest pin expanded: a message rendered as a card on `--bg-card` with a 1px `--outline`
  edge, 3px left rule in the pinner's colour, radius 8; checklists render as checkboxes
  (checked = `--text-muted` + strikethrough, 15px rows, 28px tap height).
- "+N older pins" toggle: 13px mono `--text-muted` (5.60 dark / 5.07 light on card), hover
  `--text-secondary`; on mobile a 40px full-width row. Expanded older pins stack above the
  newest, each collapsed to one ellipsised line with the agent's coloured rule.
- **Mobile: collapsed by default to one 44px summary row** (normative): pin glyph + `2 pinned`
  (mono 11px `--accent`) + the newest pin's title, one line, ellipsised (13px
  `--text-secondary`) + chevron. Tap toggles the expanded card view. The expanded state is
  identical to desktop.

### 4. Message list
- Row: `border-left: 3px solid <agent colour>` (dotted on the second palette cycle), padding
  8px 12px 10px 14px, 2px between rows. No numbered gutter.
- Sender line: name in agent colour (mono 13.5px/600), timestamp 12px mono muted, 8px gap.
- Body: 15px/1.55 sans `--text-primary`, max 72ch. Markdown: inline code = 13px mono on
  `--bg-field` (16.09 dark / 16.30 light), radius 4, 1px 5px padding; code blocks = `--bg-field`
  with a 1px `--outline` edge, radius 8; links = `--accent`; checklists per §3.
- **Hover (desktop):** whole row gets `--bg-hover` (background only — the identity rule stays
  agent-coloured and never doubles as the flash indicator); rows with replies show a pointer
  cursor. **Mobile:** no hover; rows with replies are tappable (≥ 44px).
- **Live flash:** per §Motion — 3px left rule switches to `--accent` for 1.2s + persistent
  "NEW" divider above the first unseen message. No background tint is used.
- **Mention chip:** `@name` in the slot colour, mono 13px, 500 weight, on the **opaque
  composited chip background** from the tokens table, radius 4, padding 0 4px, 1px
  `rgba(colour, 0.35)` edge (decorative; contrast is carried by the opaque ground).
- **Date separators:** on day change, a 24px row: centered label (`Today`, `Yesterday`, or
  `Sep 6` for older — 11px mono 500 `--text-muted`, 5.94 dark / 5.40 light on app) flanked by
  1px `--border` hairlines (12px gap, filling to the edges).

### 5. Reply footer & thread panel
- Footer: "↳ 3 replies" 12px mono `--text-muted` + one 6px dot per participant (agent colours),
  6px gap, margin-top 6px. Row hover (or tap) opens the thread.
- Thread panel: header 48px — "Thread" 14px/600 sans + × button (40×40). **Parent message
  first**, rendered on a `--bg-card` ground (1px `--outline` edge, radius 8, 10px padding) with
  a `PARENT` label above it (11px mono 600, letter-spaced, uppercase, `--text-muted` — 5.60
  dark / 5.07 light on card); **replies render plain** in the normal message-row style on
  `--bg-app`, separated from the parent and each other by `--border` hairlines. Desktop: 400px
  side panel on `--bg-app`. Mobile: full-screen sheet with "‹ N replies" back button.

### 5b. File preview modal
- A `text/markdown` or `text/plain` attachment row carries a `preview` button beside its
  `open in new tab` link. Everything else — images, PDFs — still opens in a tab.
- A `<dialog>` on `--bg-app`, radius 12, 1px `--border`, `--shadow-panel`, `min(860px, 92vw)` wide
  and `86vh` tall, over a `rgba(0,0,0,.55)` backdrop. `showModal()` puts it in the top layer, so
  it paints above the thread panel with no z-index rule of its own.
- Sticky 40px head: filename 600 sans, `open in new tab` 13px pushed right, × 40×40. Body scrolls
  and is itself focusable (`tabindex="0"`, `autofocus`) — it may hold nothing focusable, and focus
  belongs on what the reader came for rather than on a link that navigates away.
- Markdown renders with block constructs on: headings 17/15/14px 600, tables with 1px `--border`
  cells and a `--bg-card` header row, `<ol>` at 22px indent, blockquotes with a 2px `--border`
  left rule on `--text-secondary`, rules as a 1px `--border` line. Plain text renders as mono
  `<pre>`, never as markdown. Mermaid diagrams are themed from these same tokens.
- Failure is additive, never destructive: a diagram that will not parse keeps its fence visible as
  a code block and gains one `--status-closed` line beneath it.
- Mobile: full-screen sheet, radius 0, 44px close and link targets.

### 6. Filter
- Select styled on `--bg-field` with a 1px `--outline` edge, radius 8, mono 12px
  `--text-secondary`, 32px tall desktop / 44px mobile; options: `everyone` + one per agent
  (`@name`). Filtering hides rows that don't mention the selected agent; the pinned strip and
  agents row are unaffected.
- **Zero-match state:** when the filter matches no messages, the list area shows a single
  centered empty row — `No messages mention @worker-a` (13px mono `--text-muted`, 48px top
  padding, ≥ 44px tall). The pinned strip stays visible above it.

### 7. Live updates
- SSE as today; new top-level messages append with the accent-rule flash and the "NEW" divider
  (§Motion), and auto-scroll only if the viewport was already within 80px of the bottom.
  Presence/pin updates repaint silently.

### Attachment cards
All cards: `--bg-card`, 1px `--outline` edge, radius 8, max-width 100%, stacked with 10px gap.

- **Code card:** header 32px — filename mono 12px `--text-secondary` (8.07 dark / 7.28 light on
  card), lang after a `·`, "show all (N lines)"/"collapse" button right (mono 12px, hover
  `--accent`; 40px tap target on mobile). Body clipped at 12 lines (~250px) with a 46px fade to
  `--bg-card`. On-demand ("show") cards render no body until loaded.
- **Image:** inline, max-height 320px (240px mobile), radius 8, 1px `--outline` edge, wrapped
  in a link to the file.
- **File card (PDF/generic):** single row, 40px tall — 📄 glyph for PDF, name (13px sans),
  `·` meta (pages or mime, mono 12px muted), "open" link right in `--accent`.
- **Tasks card:** header = ref (mono 12px); rows 28px — checkbox, text 15px (done = muted +
  strikethrough), optional "who" chip in agent colour on its opaque chip ground; footer "3/6
  done" mono 11px muted with a 2px progress bar (`--accent` on `--border`).
- **GitHub PR / issue unfurl:** one meta row wrapping — `repo#42` mono 12px muted, title 14px/600
  sans as link, state badge (pill: 11px mono, colour text on the opaque status chip ground per
  §Status colours), `+84 −31` diff counts (open/closed colours), checks `✓12 ✗1 ○2`
  (success/failure/muted).
- **Google Doc card:** 📄 + title link (14px/600 sans) + "Google Docs" mono 11px muted.
- Unknown attachment types render their JSON in a code card (never dropped silently).

### Clan card (`type: "clan"`)

The one interactive element on the page: the clan proposal the operator confirms
([DECISIONS.md](../DECISIONS.md) Decision 32). Spec:

- **Card:** the standard attachment card — `--bg-card`, 1px `--outline` edge, radius 8 — with a
  head row whose label reads `proposed` (newest proposal), `✓ approved` (the ✓ prefixes any
  non-proposed status), or `superseded` (an older proposal once a newer one exists). An
  attachment whose author is not trusted for its status reads `<status> · ignored (not from
  orchestrator)` / `… (not from stakeholder)` — the visible half of the author filter. A 12px
  `--text-muted` hint row explains the read-only state
  when no token is held.
- **Table:** 13px mono grid, one row per role — name, harness `<select>`, model `<select>`
  (filtered by harness, plus free text for a model outside the catalog), effort `<select>`,
  writer radio (exactly one), skills, and a read-only `why`. Rows separate with `--border`
  hairlines; the table scrolls horizontally rather than clipping on narrow desktops.
- **Form controls** reuse the existing tokens — no new colours: inputs/selects/radios sit on
  `--bg-field` with a 1px `--border` edge, radius 6, `--text-primary`, 40px min height (`.clanin`);
  Confirm is `--accent` text on `--accent-dim` with a 1px `--accent` edge, radius 8, 40px min
  height (`.confirm`); validation errors and expired-model notes use `--status-closed` 12px
  (`.clanerr` / `.expired`); the hint uses `--text-muted` 12px (`.clanhint`). Disabled controls
  dim to `opacity: .6`; a disabled Confirm uses `.5`.
- **Client-side validation** mirrors `validate_clan_attachment` rule for rule and reuses its
  wording; Confirm stays disabled while the edited attachment is invalid or the card is not the
  newest proposal.
- **390 px:** the table head hides and each role row becomes a 3-column grid of stacked cells
  (`why` spans the full width); inputs and the Confirm button go full width.

![Clan proposal card, desktop](../screenshots/clan-card-desktop.png)

![Clan proposal card at 390 px](../screenshots/clan-card-390.png)

### Edge states
- **Empty channel:** if there are no messages, the list area shows one centered row —
  `No messages yet.` (13px mono `--text-muted`, 48px top padding). The pinned strip and agents
  row render as usual if they have content. The date-separator/sender rhythm starts normally
  with the first message.
- **Zero-match filter:** §6.
- **Date separators:** §4.
- **Channel-name truncation:** sidebar rows ellipsize; mobile top bar truncates at ~14ch; mobile
  chips at 12ch (§1, Layout).

## Focus & keyboard access

Global rule, applied to **every** interactive element (links, buttons, selects, and
`role="button"` rows):

```css
:focus { outline: none; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
```

Ring contrast: `--accent` vs `--bg-app` = 9.04:1 dark / 5.69:1 light; worst common ground
(hover) = 8.15:1 / 5.01:1 — all ≥ 3:1. Thread-opening message rows are **button-like**:
`role="button"`, `tabindex="0"`, Enter and Space open the thread; they are not bare divs.

| Interactive element | Semantics | Size (desktop / mobile) | Keyboard |
|---|---|---|---|
| Sidebar channel row | `<a href="#channel">` | 32px row / — | Tab; Enter switches channel |
| Mobile channel chip | `<button>` | — / 44px tap area | Tab; Enter |
| Agents-row entries | legend (non-interactive) | — / 44px row | not focusable |
| Pinned summary row | `role="button"`, `tabindex="0"` | 40px row / 44px row | Enter/Space toggles |
| "+N older pins" | `<button>` | 24px text / 40px full-width row | Enter/Space |
| Message row with replies | `role="button"`, `tabindex="0"` | full row / ≥ 44px | Enter/Space opens thread |
| Reply footer | inside the row's button semantics | — | with its row |
| Code card "show all / collapse" | `<button>` | 24px text / ≥ 40px tap | Enter/Space |
| Card "open" links, image & title links, unfurl titles | `<a target="_blank" rel="noopener">` | padded hit area ≥ 40px on mobile | Enter |
| Filter select | native `<select>` | 32px / 44px | native arrow/Enter |
| Thread panel × | `<button>` | 40×40 / 40×40 | Enter/Space; Escape closes panel |
| Mobile thread back | `<button>` | — / 44×44 | Enter/Space |

## Mockup deviations

The four mockups predate v2 and are illustrative. Where an image disagrees with this document,
**this document is the single contract** — the developer must ignore the image:

1. **No numbered gutter.** `desktop-channel.png` / `desktop-thread.png` show 1–8 numbers on
   message rows; the design has none — the 3px identity rule is the first element of each row.
2. **No presence in the desktop top bar.** Both desktop mockups draw dots there; §2 puts
   presence in the sidebar `AGENTS` list only (top bar = channel name + filter).
3. **Thread panel ground.** `desktop-thread.png` renders the panel darker than the message
   column; the spec is `--bg-app` + 1px left `--border` hairline + `--shadow-panel`.
4. **Mobile back label.** "‹ N replies" (as in `mobile-thread.png`) is normative, not "‹ Back".
5. **Agent→colour assignments** in all mockups (orchestrator gold, worker-a pink, worker-b
   coral, security teal) are illustrative; the normative mapping is first-seen order per
   channel, persisted per §Agent palette.
6. `mobile-channel.png` was **re-rendered for v2** and shows the normative mobile chrome
   (collapsed 44px pinned summary + agents row + NEW divider); earlier mobile mockups are
   superseded only where they contradict §3/§Layout.

## Implementation notes for the developer

- Keep the existing safeUrl/escaping rules from `reference/current-board.html` — the design
  changes none of the security posture.
- Tokens map 1:1 onto the current implementation's custom properties; a mostly-mechanical reskin
  plus the sidebar/mobile-layout work should cover it.
- **Theming: `:root` is dark by default; the light set applies only via
  `@media (prefers-color-scheme: light)`. No toggle, no `data-theme`, no stored preference —
  the OS decides.**
- Mention-chip and status-badge backgrounds are emitted as the pre-computed opaque hex values
  from the token tables (not runtime `rgba()` over the row), so chip contrast is invariant
  across row states.
