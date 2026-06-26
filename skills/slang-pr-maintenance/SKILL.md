---
name: slang-pr-maintenance
license: MIT
description: "Surface the open shader-slang PRs needing human attention with a bundled gh-only Python script (scripts/pr_report.py): a board-free, once-daily assignee-grouped escalation report computed entirely from live GitHub state (no ProjectsV2 access, no writes; optional Discord mentions). A separate script (scripts/pr_sweep.py) is the ProjectsV2 state machine that classifies each PR, advances its board Status, and assigns owners + reviewers. Use for PR triage, the reviewer-attention report, reviewer assignment, stale-PR follow-up, or a scheduled PR sweep."
provides: []
argument-hint: "report: [--recipient-map PATH] | state machine: [--apply | --replay] [--maintainer LOGIN]"
allowed-tools: Bash Read Grep Glob
---

# Slang PR Maintenance

Two cooperating, deterministic engines that share one library; the agent only
surfaces the emitted report.

- **`scripts/pr_report.py` (default)** — the **board-free escalation report**.
  Computes a once-daily, assignee-grouped report of open PRs needing human
  attention entirely from **live GitHub state**. It reads no GitHub ProjectsV2
  and writes nothing to GitHub.
- **`scripts/pr_sweep.py`** — the **ProjectsV2 state machine**. Drives every
  open PR one step toward its correct board `Status`, classifies its `Source`,
  and assigns owners + reviewers (ProjectsV2 read+write).
- **`scripts/pr_common.py`** — the shared library (config + constants, the
  `gh` I/O layer, PR/CI/review collection, source classification, committer
  signal). `scripts/pr_signal.py` holds the committer-signal ranking.

> The report and the state machine are split so the report can run without any
> ProjectsV2 scope, in preparation for the state machine being adapted into
> GitHub Actions. All GitHub work goes through `gh`; there is no dependency on
> MCP, Discord, or a nanoclaw container.

## Quick start

Everything except the flags (org, board id, teams, status/source names,
thresholds) is a constant at the top of `pr_common.py`; edit it there if it ever
moves. The board id (`PVT_kwDOAb2kZs4BSJKy`, "Slang PR Tracking") is hard-coded
and used only by the state machine.

The **state machine's** fallback assignee (the current Slang Maintainer) is
resolved automatically: the sorted-first member of the `shader-slang/slang-maintainer`
team, else `bmillsNV` if that team is empty/unset, so a PR is never left
unassigned. `--maintainer LOGIN` is an optional override of that resolution. The
**report** never predicts owners and does not special-case the maintainer (see
the Unassigned group below).

**Report (default) — board-free, no GitHub writes:**

```bash
# Render the assignee-grouped escalation report with notifying mentions.
python3 scripts/pr_report.py --recipient-map <path>

# No mapping file (absent / testing): every login renders as inert `backticks`
# so nobody is pinged. Otherwise identical.
python3 scripts/pr_report.py
```

The report reads only live GitHub state, persists its own local state file
(`./.pr-sweep-state.json`: per-PR stall clocks + `last_report_at`), and exits
`10` when the daily report is due to be surfaced (`0` otherwise).

**State machine — ProjectsV2 read+write:**

```bash
# One-shot: compute a plan and apply it (board writes). Scheduled runs use this.
python3 scripts/pr_sweep.py --apply

# DRY RUN / debugging — no flags: compute, print the summary, and write a
# replayable plan to ./.pr-sweep-plan.json. No GitHub writes.
python3 scripts/pr_sweep.py

# REPLAY — apply the last saved plan as-is, without recomputing.
python3 scripts/pr_sweep.py --replay

# Override the auto-resolved fallback assignee (rarely needed):
python3 scripts/pr_sweep.py --maintainer <login> --apply
```

The plan/apply split lets a maintainer eyeball (or diff) `./.pr-sweep-plan.json`
before anything is written — especially useful for the large first run.
**One-shot is literally plan + apply** (`run_sweep` is side-effect-free apart
from writing the plan file; a single apply step performs the GitHub writes), and
idempotency means a later one-shot self-corrects any drift.

**These are long-running processes — do not kill them.** An org-wide pass takes
a few minutes (≈5-6s per repo page; the state machine additionally runs a
one-time owner-ranking query per unassigned PR on a backlog run — the report
does not). Both stream progress to **stderr** (a start banner, a line per repo
page) so they are never silent; **stdout** carries only the summary + report.
Run with a generous timeout and treat the stderr heartbeats as liveness.

Under WSL both scripts prefer `gh.exe` and stop if `gh` is missing rather than
falling back to a different toolchain.

## What the report does (`pr_report.py`)

1. **Collect** (batched `gh` GraphQL): **one paginated query per repo**
   (`DEFAULT_PR_PAGE_SIZE`, default 25) returns every open PR with everything
   needed in a single shot — core fields, author type, assignees, requested
   reviewers, CI (`statusCheckRollup` → `ci_state`), the head commit date, reviews
   (→ `last_review_at` / `change_requested` / `approved`), `mergeQueueEntry`,
   linked-issue assignees, and changed files (for signal). **No ProjectsV2 query.**
2. **Synthesize** (pure, in-memory): classify each PR's `Source` live, derive
   its lifecycle stage from the collected signals (see the mapping below), update
   each PR's stall clock, and build the assignee-grouped report from the
   per-source ladders. The report does not predict owners — a PR with no human
   assignee is grouped under **Unassigned** (so changed files are read only for
   `Source` classification context, not for owner ranking).
3. **Emit**: a human summary + the assignee-grouped report on stdout, and
   persist the local state file. Exit code `10` means "the daily report is due
   to surface"; `0` means nothing to surface.

### ProjectsV2 → live-state derivation

The report previously read three per-PR fields from the board. It now re-derives
(or drops) each from the live PR query — see the docstrings in `pr_report.py`:

- **`board_status` → `target_status(pr, cfg)`** (in `pr_common`). The board
  `Status` is just a cached reconciliation of draft/CI/review/merge-queue signals,
  all present in the live query — and `target_status` is the same single source of
  truth the state machine and the GitHub workflow use, so the report's stage
  matches the board exactly (`In Review` / `Revising` / `Snagged` / `Approved`;
  the report never observes the terminal `Done` since it lists only open PRs). See
  the State machine section below for the full priority order.
- **`source` → `classify_source` (live, every run):** `Bot` if a bot author,
  else `Internal` if the author can commit to the repo, else `Community`. A
  manual board `Source` override is ignored (re-classified live).
- **`project_item_id` → dropped** (only the state machine needs it, for writes).

`target_status` also replaces `board_status` in the stall **move fingerprint**,
so a PR's CI going green or an approval landing counts as movement, without the
board.

## What the state machine does (`pr_sweep.py`)

1. **Collect**: the same batched per-repo query **plus** the board `Status` +
   `Source` per PR (a ProjectsV2 query).
2. **Synthesize**: resolve each PR's `Source` (board-authoritative when set,
   classify when empty), compute the single board transition it warrants, pick
   the owner + reviewers when needed, and emit a self-contained, replayable
   **plan** (`./.pr-sweep-plan.json`).
3. **Act** (`--apply`, or `--replay` for a saved plan): execute the plan — set
   `Source` (when newly classified), set `Status`, set assignee, request reviewers
   — each idempotent (never repeats an action whose effect is already present).
   The sweep never comments on PRs.

### State machine (board `Status` field)

The lifecycle stage is computed by `target_status` in `pr_common` — the **single
source of truth**, shared by the state machine and the board-free report, and
kept **identical to `computeTarget`** in the event-driven GitHub workflow
(`.github/workflows/pr-board-sync.yml` in the slang repo). Five states, two
variants that differ **only on a CI failure**:

- **In Review** — default for an open PR (awaiting review / CI pending / a fresh
  commit with no feedback yet); a **Bot draft** lands here so a human owner can
  see/shepherd it. This + the board view IS the reviewer's "needs review" signal.
- **Revising** — a **human draft**, or a reviewer requested changes (not yet
  superseded by a newer commit); a **Bot** PR's CI failure also lands here.
- **Snagged** — needs a human: a **human** PR's CI failed, CI is awaiting a
  maintainer's approval to run (`action_required`, e.g. a fork PR), or an approved
  PR has green CI but is **not** in the merge queue (a human must enqueue/merge it).
- **Approved** — not a draft, already approved, waiting on CI / the merge queue.
- **Done** — the PR is closed (merged or otherwise); terminal.

`target_status` priority (first match wins): a current changes-request →
`Revising`; else draft → `In Review` (Bot) / `Revising` (human); else CI
`action_required` → `Snagged` (always); else CI failed → `Revising` (Bot) /
`Snagged` (human); else approved → `Snagged` if green & un-queued else `Approved`;
else `In Review`. A review opinion counts only if it was made on the **current
head commit** (`commit.oid == headRefOid`); an opinion on an earlier commit is
superseded by a newer push (→ `In Review` until re-reviewed).

**Reconciliation (board vs reality).** The sweep does not special-case
"contradictory" states: `reconcile` recomputes `target_status` from observed
reality and corrects the board toward it, so drift is fixed by construction. The
only non-derived cases are **off-board PRs** (added via `addProjectV2ItemById`,
fields set the same pass) and **merged/closed PRs** (→ `Done`; the sweep lists
only open PRs, so the event workflow normally sets this). The merge-queue **drop**
is not observable to the sweep, but a dropped PR reads approved + green +
un-queued, which `target_status` maps to `Snagged` — so the sweep agrees with the
workflow's `dequeued -> Snagged` instead of reverting it.

## PR sources (`Source`) and per-source behavior

Every PR carries a **`Source`** — **`Internal` / `Community` / `Bot`**. The
state machine reads it from the board (authoritative when set) and classifies +
sets it when empty; the report always classifies it live. Classification: `Bot`
if the author is a bot (`DEFAULT_BOT_AUTHORS`), else `Internal` if the author can
commit to the target repo, else `Community`. Behavior differs by source:

| Behavior | **Internal** | **Community** | **Bot** |
|---|---|---|---|
| Assignee | the **author** (drives it) | signal owner from `pr-owners` | signal owner from `bot-pr-owners` |
| Board lifecycle (state machine) | yes | yes | yes |
| Auto-request reviewer (top collaborator-not-owner) | **no** (author picks own) | **yes** | yes |
| Report predicate ladder | **none** (excluded) | `COMMUNITY_LADDER` (incl. `needs CI approval`, `changes requested`) | `BOT_LADDER` (no CI-approval/changes rungs) |
| Drafts | `Revising`, exempt from assignment | `Revising`, exempt from assignment | **not exempt → `In Review` + owner** |
| CI failure (non-draft) | `Snagged` | `Snagged` | `Revising` (bot fixes itself) |

**One-liner:** Internal = *self-managed*; Community = *bot-managed oversight*;
Bot = the bot lane (a bot PR, including a draft, sits in `In Review` with an owner
+ reviewer so a human shepherds it to ready). Human drafts sit in `Revising` and
are exempt from assignment. Internal vs. Community is decided by **repo write
access** (can the author approve workflow runs / approve+merge PRs), matching the
event workflow's classifier.

Assignee chain for Community/Bot (the source picks the owner pool): an owner
assigned to the PR's linked issue → highest-signal committer who is an owner →
the resolved maintainer (the `slang-maintainer` team's sorted-first member, else
`bmillsNV`). This chain is the **state machine's** (it writes the assignee);
the report does not predict — it shows whatever assignee GitHub currently has,
and groups the rest under **Unassigned**. Blocking review feedback returns any
PR to `Revising` (state machine).

Reviewers (Community/Bot only) = the assignee plus the single highest-signal
committer who is a **write+ collaborator of the PR's repo** (`push`/`maintain`/
`admin`) but not in the owner pool; the PR author is never requested. Two rules
keep the request meaningful:

- **No piling on:** if the PR already has a *real* reviewer requested, no
  reviewer is added. "Real" excludes bots and `DEFAULT_IGNORED_REVIEWERS`
  (auto-assigned non-approvers, e.g. `bmillsNV`).
- **Unrequest the placeholder:** when the sweep assigns a PR, it unrequests any
  `DEFAULT_IGNORED_REVIEWERS` that GitHub auto-added.

**Committer signal (weighted).** Owners/committers are ranked by a weighted file
signal, not raw recency:

1. Per-file signal = `max(additions, deletions)` of the PR's change to that file
   × `file_multiplier(path)` (directory-glob table; longest match wins, default
   `1.0`).
2. Take the **top `DEFAULT_SIGNAL_TOP_FILES`** (default 10) files by per-file
   signal and normalize them into relative **weights** (sum = 1).
3. **Cheap pass** — a **single batched GraphQL query** fetches, per top file, the
   last `DEFAULT_SIGNAL_COMMITS` (default 5) default-branch commits within
   `DEFAULT_SIGNAL_HORIZON_DAYS` (default 180), each with its `oid`, author,
   commit-total `max(add, del)`, and the associated PR's `APPROVED` reviews. Each
   commit's LOC is attributed to its **author** — or, if the author is a bot,
   unmapped, or **the current PR's author**, to the **last `APPROVED` reviewer**
   of the PR that introduced it (dropped if none). Overall signal =
   Σ over files of `weight(file) × LOC(committer, file)`; ranked highest-first.
4. **Tiebreak (hybrid)** — when the cheap top two are *close* (within
   `DEFAULT_SIGNAL_CLEAR_MARGIN`, default 1.5×), re-score just the top
   `DEFAULT_SIGNAL_FINALISTS` (default 6) candidates using **true per-file LOC**
   and re-order them. Skipped on a clear winner.

A **run-scoped cache** holds the PR-independent *source* data (each file's
default-branch history; each commit's per-file stats) so a hot file or shared
commit is fetched once per run. The ranking lives in
[scripts/pr_signal.py](scripts/pr_signal.py); both entrypoints call it via
`pr_common.collect_committer_signal`.

- **Draft human PRs are fully exempt** from the report and from state-machine
  assignment (draft = the author's "not ready" signal).

## Notification model: assignee-grouped report

The project-board inbox views are the passive reviewer inbox. Routine "ready for
review" is conveyed by `In Review` + assignee — not a message.

The report (`pr_report.py`) emits **one report, grouped by assignee**, surfaced
at most once per `DEFAULT_REPORT_INTERVAL_HOURS` (daily). Each section is one
assignee's queue; PRs with no human assignee are grouped under **Unassigned**,
listed **first**. The maintainer is not special-cased — they appear under their
own login like any assignee. An item that passes the escalate rung is marked
overdue **in place** — it stays under its assignee/Unassigned group and gains
the `⬆️` marker. Example:

```
## Slang PR Escalation Report

- **Unassigned**:
  - ⬆️ 🌐 [slang#334](…/pull/334) — needs CI approval
  - 🤖 [slang#9001](…/pull/9001) — awaiting review from: <@222>
- **`alice`**:
  - 🌐 [slang#777](…/pull/777) — changes requested — check if author is still active / needs help
```
- The report is titled **"Slang PR Escalation Report"**.
- **Unassigned** (PRs with no human assignee, incl. bot-only like `Copilot`) is listed first; named assignees follow, sorted. Escalations are marked identically in every group.
- **Within each group**, items are ordered Community (`🌐`) before Bot (`🤖`), and within each source escalated (`⬆️`) before not-escalated.
- `⬆️` marks an item **escalated/overdue** past the second (escalate) rung.
- `🌐` Community, `🤖` Bot. Internal PRs and human drafts are excluded. PR refs are clickable links.
- **Mentions** (`--recipient-map`): a login present in the supplied map renders as a `<@id>` mention that pings on Discord (the format also fits Slack); every other login renders as inert `` `login` ``. **The invoker must pass `--recipient-map PATH`** to get pings. See the schema below.

### Per-source predicate ladders (the single source of truth)
Each PR's **reason** is the first matching predicate in its source's ladder; its
**stall** (working-hours since it last *moved* — derived stage change / new
commit / new review) selects the rung: it surfaces under its assignee/Unassigned
group once `stall >= assignee_after`, and is marked overdue in place (`⬆️`) once
`stall >= escalate_after`. Defined in `COMMUNITY_LADDER` / `BOT_LADDER` in
[scripts/pr_report.py](scripts/pr_report.py):

- **Community:** `needs CI approval` (surface 0h / escalate 24h) → `changes requested — check if author is still active / needs help` (1wk / 2wk) → `awaiting review from: …` (24h / 48h) → `CI failing — needs fixes` (24h / 48h) → `idle for N days` (24h / 48h).
- **Bot:** `awaiting review from: …` (48h / 1wk) → `CI failing — needs fixes` (48h / 1wk) → `idle for N days` (48h / 1wk). No `needs CI approval` or `changes requested` rung.

Edit the ladders to retune timeouts/audiences. The report is a **current-state**
list: an item keeps appearing until the PR moves (which resets its stall clock);
the daily cadence is the throttle. "awaiting review" only fires when the PR has
reached the derived `In Review` stage (not yet approved) and a real
(approve-capable) reviewer is requested — auto-assigned non-approvers
(`DEFAULT_IGNORED_REVIEWERS`, e.g.
`bmillsNV`) and bots are excluded.

### Surfacing the report (agent's job, method-agnostic)

The script only **emits** the report (stdout + exit code `10` when due). This
skill does NOT prescribe delivery — the agent uses whatever channel is available
at runtime. Always keep the bot-transparency disclaimer the script appends. To
make the report **notify** people (e.g. on Discord), pass `--recipient-map PATH`.

### Recipient map (`--recipient-map`)

A flat JSON object mapping **GitHub login -> destination user ID** (matched
case-insensitively):

```json
{ "jhelferty-nv": "123456789012345678", "bob": "987654321098765432" }
```

- A login in the map renders as `<@id>` (pings on Discord; the shape also fits
  Slack). Any login **not** in the map (or when no file is passed) renders as
  inert `` `login` `` so it can never notify the wrong person.
- The path is supplied by the invoker **each run**; there is no auto-discovery.
- The mapping affects the **report text only**. All routing, assignment writes
  (state machine), stall state, and bot detection stay on GitHub logins.

## Agent's residual job

1. Run `scripts/pr_report.py --recipient-map <path>` (the default). When the
   exit code is `10`, surface the report to its recipients (method-agnostic).
2. Optionally run the state machine `scripts/pr_sweep.py --apply` to keep the
   board reconciled (on a backlog run, plan first with no flags, eyeball
   `./.pr-sweep-plan.json`, then `--replay`).

Everything else is the scripts'.

## Configuration (top-of-file constants in `pr_common.py`)

The flags are `--apply` / `--replay` and the optional `--maintainer LOGIN`
override (state machine only), and `--recipient-map PATH` (report only). The
fallback assignee otherwise resolves from the `slang-maintainer` team (then
`bmillsNV`). The report takes none of these. Everything else is a constant near the top of
`pr_common.py` — edit it there if it moves:

| Constant | Value | Notes |
|------|---------|-------|
| `DEFAULT_ORG` | `shader-slang` | org swept when `DEFAULT_REPOS` is empty |
| `DEFAULT_REPOS` | _(empty)_ | comma-separated `owner/name` subset; empty -> every non-archived repo in the org |
| `DEFAULT_PROJECT_ID` | `PVT_kwDOAb2kZs4BSJKy` | the "Slang PR Tracking" board (state machine only) |
| `DEFAULT_STATUS_*` | `Status` / `In Review`/`Revising`/`Snagged`/`Approved`/`Done` | board option names (also the derived-stage names) |
| `DEFAULT_SOURCE_*` | `Source` / `Internal`/`Community`/`Bot` | source-classification option names |
| `DEFAULT_OWNERS_TEAM` | `shader-slang/pr-owners` | **Community** assignee pool (parent team; includes the `bot-pr-owners` subteam) |
| `DEFAULT_BOT_OWNERS_TEAM` | `shader-slang/bot-pr-owners` | **Bot** assignee pool |
| extra-reviewer pool | per-repo | write+ collaborators of each PR's repo (`permissions.push == true`); members not in the owner pool |
| `DEFAULT_FILE_MULTIPLIERS` | _(built-in table)_ | directory-glob -> weight; longest match wins, else `DEFAULT_FILE_MULTIPLIER` |
| `DEFAULT_SIGNAL_TOP_FILES` | `10` | top files (by per-file signal) used to rank committers |
| `DEFAULT_SIGNAL_COMMITS` | `5` | commits sampled per file for committer signal |
| `DEFAULT_SIGNAL_HORIZON_DAYS` | `180` | only consider commits within this window |
| `DEFAULT_SIGNAL_FINALISTS` | `6` | top candidates re-scored by true per-file LOC in the tiebreak |
| `DEFAULT_SIGNAL_CLEAR_MARGIN` | `1.5` | skip the tiebreak when #1's signal >= margin x #2's |
| `DEFAULT_BOT_AUTHORS` | `nv-slang-bot,slang-coworker-nanoclaw,Copilot,copilot-swe-agent` | bot logins matched by name (GitHub's `is_bot`/`__typename` is also honored for authors) |
| `DEFAULT_IGNORED_REVIEWERS` | `bmillsNV` | auto-assigned reviewers that can't approve; unrequested on assignment and ignored when checking existing reviewer coverage |
| `DEFAULT_REPORT_INTERVAL_HOURS` | `24` | the report is surfaced at most this often (daily) |
| `DEFAULT_WORKDAY_TZ` | `America/Los_Angeles` | timezone for the workday model (stall clock skips weekends) |
| `PLAN_FILE` | `./.pr-sweep-plan.json` | the state machine's computed, replayable plan |
| `STATE_FILE` | `./.pr-sweep-state.json` | the report's per-PR stall clocks (`move_fingerprint` + `last_moved_at`) and `last_report_at` |
| `DEFAULT_PR_PAGE_SIZE` | `25` | PRs per batched GraphQL page (capped by server timeout: n=50 returns HTTP 504, n=25 resolves in ~5-6s) |

## Prerequisites

`gh` authenticated (`gh auth status`); the scripts fail loudly if it is missing.
Required access differs by script:

**Report (`pr_report.py`) — no ProjectsV2, no writes:**

- **repo read** for the PR/CI/review/linked-issue GraphQL query (classic `repo`
  scope covers private repos).
- **repo push access** to list the write+ collaborator pool
  (`repos/{repo}/collaborators`) — used for live `Source` classification
  (Internal iff the author can commit). On error the pool is empty (the PR
  classifies as `Community`); the report still runs.
- **No ProjectsV2 scope, no write scopes, and no `read:org`** are needed — the
  report does not predict owners, so it reads no team membership.

**State machine (`pr_sweep.py`):** everything above **plus `read:org`** (owner
+ `slang-maintainer` team membership for assignee selection), **ProjectsV2 read**
(board `Status`/`Source`), and — for `--apply`/`--replay` — **repo write**
(issues/comments/assignees) **+ ProjectsV2 write**.

A local clone is NOT required (history / board access go through `gh api`);
running inside a checkout is only a fast path.

## Scheduling

Any scheduler works (cron, CI, or — in nanoclaw — a `schedule_task`). Run the
**report** (`pr_report.py --recipient-map <path>`) on a cadence; it
self-throttles to once per `DEFAULT_REPORT_INTERVAL_HOURS` (daily) and exit code
`10` means "the report is due to surface." Run the **state machine**
(`pr_sweep.py --apply`) ~every 30-60 min to keep the board reconciled. The state
machine is being adapted into GitHub Actions; the report stays a standalone,
board-free run.

## Tests

```bash
python3 scripts/test_pr_sweep.py    # state machine + shared library + signal
python3 scripts/test_pr_report.py   # board-free report (ladders, stall clock, routing)
```

Cover the pure decision functions with no live `gh` calls: bot + source
classification, the board state machine, the per-source stage derivation, the
predicate ladders + assignee-grouped report routing, the movement/stall clock,
CI summarization, the weighted committer signal, and assignee/reviewer
selection.
