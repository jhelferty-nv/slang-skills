# PR maintenance in GitHub Actions

The **event-driven** half of the slang PR board state machine now lives as a
**reusable GitHub Actions workflow** in the `shader-slang/slang` repo, so every
repo in the org can reconcile its PRs onto the shared "Slang PR Tracking" board
the instant something happens — no polling, no per-repo logic to maintain.

The committer-signal **owner/reviewer assignment** and full **drift
reconciliation** stay in [`scripts/pr_sweep.py`](../scripts/pr_sweep.py), which
runs on a cadence and is idempotent. The two are complementary: Actions handle
the fast, observable happy path; the sweep fills assignees/reviewers and repairs
anything events could not see (fork-PR CI, missed webhooks, repos not yet
onboarded).

## What moved into Actions

Implemented in `shader-slang/slang/.github/workflows/pr-board-sync.yml`:

| Activity | Trigger | Mirrors in `pr_sweep.py` |
|---|---|---|
| Add PR to the board | `pull_request_target: opened` | `add_to_project` |
| Set `Source` (Internal / Community / Bot) | `pull_request_target: opened`/`reopened` | `set_source` / `classify_source` |
| Entry status -> `Revising` | `pull_request_target: opened` | `_normal_lifecycle` initial status |
| Merged/closed -> `Done` | `pull_request_target: closed` | `_correct_terminal_state` |
| Human draft -> `Revising` | `pull_request_target: converted_to_draft` | `_correct_misplaced_draft` |
| Changes requested -> `Revising` | `pull_request_review: submitted` | `change_requested` branch |
| CI failed -> `Revising`; CI passed + not draft -> `Todo` | `check_suite: completed`, `ready_for_review` | `_correct_failing_ci` + promotion |
| Unrequest ignored reviewers (e.g. `bmillsNV`) | `opened` / `reopened` / `ready_for_review` / `review_requested` | `remove_reviewers` |

What stays in `pr_sweep.py`: `set_assignee` + `request_reviewers` (need the
weighted committer-signal git-history ranking in `pr_signal.py`), the one-time
"ready" comment, and the periodic board-vs-reality reconciliation safety net.

## How cross-repo reuse works (confirmed)

GitHub supports two reuse mechanisms across repos in an org:

- **Reusable workflows** — `uses: <owner>/<repo>/.github/workflows/<file>.yml@<ref>`
  referenced from another repo's job. The PR event triggers must be declared in
  the *calling* repo's workflow (a reusable workflow cannot declare `on:
  pull_request*` itself), which is why each consuming repo keeps a thin caller.
- **Composite actions** — `uses: <owner>/<repo>/.github/actions/<name>@<ref>` as
  a step inside a job.

This skill uses **reusable workflows**: the central logic + board IDs + the
`github-script` steps live once in `shader-slang/slang`, and each repo adds only
triggers + a single mapped secret.

## Onboarding another repo

1. Copy [`example-caller.yml`](example-caller.yml) to the repo as
   `.github/workflows/pr-maintenance.yml`.
2. Done — the defaults already target the shared board, and the one secret
   (`SLANG_PR_BOT_TOKEN`, a dedicated bot PAT) is a shader-slang **org-level**
   secret shared org-wide; the caller just maps it. The reusable workflow uses
   that token for every call (no `permissions:` block needed), so its capabilities
   can grow without changing this caller. Override an input via a `with:` block
   only if a value ever changes.

`shader-slang/slang` itself consumes the same reusable workflow (via the local
`./.github/workflows/pr-board-sync.yml` path) so there is a single source of
truth.

## Notes / trade-offs

- **`check_suite: completed` volume.** On a high-CI repo this fires often; each
  run is a cheap metadata-only job. To disable CI-driven status transitions
  while keeping the rest, drop the `check_suite` trigger from the caller.
- **Fork PRs.** `check_suite` does not link fork PRs, so their CI-driven status
  changes are picked up by the periodic `pr_sweep.py` reconcile instead.
- **`pull_request_target`.** Used so secrets are available for fork PRs. Every
  job is metadata/API only and never checks out or runs PR code.
