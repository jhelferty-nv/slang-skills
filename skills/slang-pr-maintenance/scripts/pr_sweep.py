#!/usr/bin/env python3
"""Reconcile open PRs on a GitHub ProjectsV2 board toward their correct state.

This is the **state-machine** half of the slang-pr-maintenance skill: a single
sweep (intended to run ~every 30-60 min) drives every open PR one step toward
its correct board Status, classifies its Source when unset, and assigns owners +
reviewers. It reads and writes GitHub ProjectsV2.

The **report** half — surfacing items needing human attention — lives in
pr_report.py and runs board-free; the two share pr_common.py. This split is in
preparation for the state machine being adapted into GitHub Actions.

The script collects PR + board + CI + review state, computes exactly one
transition per PR, and (under --apply) performs the idempotent GitHub-side
writes.

Portable: depends only on an authenticated `gh` (and, optionally, a local git
checkout as a fast path). All org and infra constants are defaults in
pr_common.py with shader-slang values.

State machine (board Status field), two variants differing ONLY on a CI failure:
    In Review -> Revising / Snagged / Approved -> Done
  - In Review: default for an open PR (awaiting review / CI pending / a fresh
               commit); a Bot draft lands here so a human owner can shepherd it.
  - Revising:  a human draft, or a reviewer requested changes; a Bot PR's CI
               failure also lands here (the bot fixes itself).
  - Snagged:   needs a human - a human PR's CI failed, or an approved PR has green
               CI but is not in the merge queue (a human must enqueue/merge it).
  - Approved:  not a draft, already approved, waiting on CI / the merge queue.
  - Done:      the PR is closed (merged or otherwise); terminal.

The lifecycle stage is computed by `target_status` in pr_common (the single
source of truth, shared with the board-free report and kept identical to
computeTarget in .github/workflows/pr-board-sync.yml). `reconcile` corrects the
board toward it. Pure decision functions are covered by test_pr_sweep.py with no
live `gh` calls; the committer-signal ranking lives in pr_signal; the shared
collection / I/O lives in pr_common.
"""
from __future__ import annotations

# Thin glue around `gh` + untyped JSON; strict "unknown/Any" rules relaxed (the
# pure decision functions are covered by test_pr_sweep.py instead).
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false, reportUnusedCallResult=false, reportImplicitStringConcatenation=false, reportUninitializedInstanceVariable=false, reportImplicitRelativeImport=false

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any, final

import pr_signal
from pr_common import (
    Config,
    Gh,
    PR,
    _progress,
    _select_assignee_reviewers,
    classify_source,
    collect_committer_signal,
    collect_open_prs,
    collect_repo_collaborators,
    find_gh,
    list_org_repos,
    list_team_members,
    resolve_maintainer,
    save_state,
    target_status,
)


# ---------------------------------------------------------------------------
# The decision shape (plain data, used by the pure reconcile logic)
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    pr: PR
    set_status: str | None = None
    set_assignee: str | None = None
    request_reviewers: list[str] = field(default_factory=list)
    remove_reviewers: list[str] = field(default_factory=list)  # unrequest (e.g. bmillsNV)
    add_to_project: bool = False

    def is_noop(self) -> bool:
        return not (
            self.set_status or self.set_assignee or self.request_reviewers
            or self.remove_reviewers or self.add_to_project
        )


# ---------------------------------------------------------------------------
# PURE LOGIC (no I/O) -- exercised directly by test_pr_sweep.py
# ---------------------------------------------------------------------------

def _ensure_assignee(d: Decision, pr: PR) -> None:
    """Always-have-an-assignee rule: when a PR is unassigned and an assignee was
    resolved (issue -> commit-signal -> maintainer), set it, request reviewers,
    and unrequest any auto-assigned non-approvers (e.g. bmillsNV)."""
    if (not pr.assignees) and pr.assignee_pick:
        d.set_assignee = pr.assignee_pick
        d.request_reviewers = list(pr.review_requests)
        d.remove_reviewers = list(pr.reviewers_to_remove)


def _reconcile_status(pr: PR, cfg: Config) -> Decision:
    """The status/assignee decision proper: derive the correct stage from
    observed reality via target_status (the shared single source of truth) and
    correct the board toward it. Human drafts are exempt from assignment (the
    author's "not ready" signal); every other PR (bot PRs incl. drafts, non-draft
    humans) gets its owner + reviewers — a no-op for already-assigned or
    merge-queued PRs, whose pick is never computed."""
    d = Decision(pr=pr)

    # Terminal: merged/closed -> Done (defensive; the sweep only lists open PRs,
    # so the event workflow normally sets this on the `closed` event).
    if pr.state in ("MERGED", "CLOSED"):
        if pr.board_status != cfg.status_done:
            d.set_status = cfg.status_done
        return d

    target = target_status(pr, cfg)
    if not (pr.is_draft and not pr.is_bot):
        _ensure_assignee(d, pr)
    if pr.board_status != target:
        d.set_status = target
    return d


def reconcile(pr: PR, cfg: Config) -> Decision:
    """Compute the single board transition this PR warrants. Idempotent: never
    proposes a write whose effect is already present.

    A PR not yet on the board is added *and* gets its initial status/assignee
    (and, via _plan_item, its Source) in the same pass: the apply step captures
    the freshly created item id, so there is no need to wait a run for the board
    item to exist before its fields can be set."""
    d = _reconcile_status(pr, cfg)
    if pr.project_item_id is None:
        d.add_to_project = True
    return d


# ---------------------------------------------------------------------------
# Plan file (the computed, replayable plan)
# ---------------------------------------------------------------------------

def save_plan(path: str, summary: dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load_plan(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Project board reads (ProjectsV2)
# ---------------------------------------------------------------------------

def _project_items_query(status_field: str, source_field: str) -> str:
    return """
query($project: ID!, $cursor: String) {
  node(id: $project) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          status: fieldValueByName(name: %s) {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          source: fieldValueByName(name: %s) {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            ... on PullRequest { number repository { nameWithOwner } }
          }
        }
      }
    }
  }
}
""" % (json.dumps(status_field), json.dumps(source_field))


def collect_board_status(gh: Gh, cfg: Config) -> dict[str, dict[str, Any]]:
    """Map "owner/repo#number" -> {status, source, item_id} for board items."""
    query = _project_items_query(cfg.status_field, cfg.source_field)
    result: dict[str, dict[str, Any]] = {}
    cursor = None
    while True:
        variables = {"project": cfg.project_id}
        if cursor:
            variables["cursor"] = cursor
        data = gh.graphql(query, variables)
        node = (((data or {}).get("data") or {}).get("node") or {})
        items = (node.get("items") or {})
        for n in items.get("nodes", []) or []:
            content = n.get("content") or {}
            if "number" not in content:
                continue
            repo = ((content.get("repository") or {}).get("nameWithOwner")) or ""
            key = f"{repo}#{content['number']}"
            result[key] = {
                "status": (n.get("status") or {}).get("name"),
                "source": (n.get("source") or {}).get("name"),
                "item_id": n.get("id"),
            }
        page = items.get("pageInfo") or {}
        if page.get("hasNextPage"):
            cursor = page.get("endCursor")
        else:
            break
    return result


# ---------------------------------------------------------------------------
# Apply (writes; only under --apply)
# ---------------------------------------------------------------------------

@final
class Applier:
    """Performs GitHub-side writes. Resolves the project Status field option ids lazily."""

    def __init__(self, gh: Gh, cfg: Config):
        self.gh = gh
        self.cfg = cfg
        # field name -> (field_id, {option_name: option_id}); resolved lazily.
        self._fields: dict[str, tuple[str, dict[str, str]]] = {}

    def _field(self, name: str) -> tuple[str, dict[str, str]]:
        if name in self._fields:
            return self._fields[name]
        query = """
        query($project: ID!) {
          node(id: $project) {
            ... on ProjectV2 {
              field(name: %s) {
                ... on ProjectV2SingleSelectField { id options { id name } }
              }
            }
          }
        }
        """ % json.dumps(name)
        data = self.gh.graphql(query, {"project": self.cfg.project_id})
        fld = ((((data or {}).get("data") or {}).get("node") or {}).get("field") or {})
        opts = {o["name"]: o["id"] for o in (fld.get("options") or [])}
        resolved = (fld.get("id") or "", opts)
        self._fields[name] = resolved
        return resolved

    def _set_single_select(self, pr: PR, field_name: str, value: str) -> None:
        if not pr.project_item_id:
            print(f"  ! {pr.key()} not on board; cannot set {field_name}", file=sys.stderr)
            return
        field_id, options = self._field(field_name)
        option_id = options.get(value)
        if not field_id or not option_id:
            print(f"  ! {field_name} option {value!r} not found on board", file=sys.stderr)
            return
        mutation = """
        mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $project, itemId: $item, fieldId: $field,
            value: { singleSelectOptionId: $option }
          }) { projectV2Item { id } }
        }
        """
        self.gh.graphql(mutation, {
            "project": self.cfg.project_id,
            "item": pr.project_item_id,
            "field": field_id,
            "option": option_id,
        })

    def add_to_project(self, pr: PR) -> None:
        if not pr.node_id:
            print(f"  ! {pr.key()} has no node id; cannot add to board", file=sys.stderr)
            return
        mutation = """
        mutation($project: ID!, $content: ID!) {
          addProjectV2ItemById(input: { projectId: $project, contentId: $content }) {
            item { id }
          }
        }
        """
        data = self.gh.graphql(mutation, {"project": self.cfg.project_id, "content": pr.node_id})
        # Capture the new item id so the same apply pass can set its fields
        # (Source/Status/...) — otherwise those writes hit the "not on board"
        # guard and are deferred a whole run.
        item_id = ((((data or {}).get("data") or {}).get("addProjectV2ItemById") or {})
                   .get("item") or {}).get("id")
        if item_id:
            pr.project_item_id = item_id

    def set_status(self, pr: PR, status: str) -> None:
        self._set_single_select(pr, self.cfg.status_field, status)

    def set_source(self, pr: PR, source: str) -> None:
        self._set_single_select(pr, self.cfg.source_field, source)

    def set_assignee(self, pr: PR, login: str) -> None:
        self.gh.run([
            "api", "-X", "POST",
            f"repos/{pr.repo}/issues/{pr.number}/assignees",
            "-f", f"assignees[]={login}",
        ], check=False)

    def request_reviewers(self, pr: PR, logins: list[str]) -> None:
        if not logins:
            return
        args = [
            "api", "-X", "POST",
            f"repos/{pr.repo}/pulls/{pr.number}/requested_reviewers",
        ]
        for login in logins:
            args += ["-f", f"reviewers[]={login}"]
        self.gh.run(args, check=False)

    def remove_reviewers(self, pr: PR, logins: list[str]) -> None:
        if not logins:
            return
        args = [
            "api", "-X", "DELETE",
            f"repos/{pr.repo}/pulls/{pr.number}/requested_reviewers",
        ]
        for login in logins:
            args += ["-f", f"reviewers[]={login}"]
        self.gh.run(args, check=False)


# ---------------------------------------------------------------------------
# Sweep orchestration
# ---------------------------------------------------------------------------

def collect_prs(gh: Gh, cfg: Config,
                repo_collaborators: Callable[[str], set[str]]) -> list[PR]:
    """All open PRs across cfg.repos, fully populated by the batched query, with
    Source resolved (board-authoritative when set; classified when not) and (for
    unassigned, non-exempt PRs) committer signal."""
    _progress(f"sweeping {len(cfg.repos)} repo(s) — this typically takes a few minutes…")
    _progress("reading project board status…")
    board = collect_board_status(gh, cfg)
    # Run-scoped cache of PR-independent source data (file history, commit stats)
    # so hot files/commits are fetched once across the whole sweep. `since` is
    # computed once here for stable cache keys.
    signal_cache = pr_signal.SignalCache()
    signal_since = pr_signal.horizon_since(cfg.signal_horizon_days)
    prs: list[PR] = []
    for repo in cfg.repos:
        for pr in collect_open_prs(gh, repo, cfg):
            entry = board.get(pr.key())
            if entry:
                pr.board_status = entry.get("status")
                pr.project_item_id = entry.get("item_id")
                pr.source = entry.get("source") or ""
            # Source is authoritative when set; classify + mark for writing when
            # not (reusing the repo's write+ collaborator set, fetched lazily).
            if not pr.source:
                pr.source = classify_source(pr, cfg, repo_collaborators(repo))
                pr.source_unset = True
            pr.is_bot = (pr.source == cfg.source_bot)

            # Committer signal feeds assignee/reviewer selection — only needed
            # for unassigned PRs that aren't human-draft-exempt or merge-queued.
            human_draft = pr.is_draft and not pr.is_bot
            if not human_draft and not pr.assignees and not pr.in_merge_queue:
                _progress(f"  ranking owner/reviewers for {pr.key()}…")
                collect_committer_signal(gh, pr, cfg, signal_cache, signal_since)
            prs.append(pr)
    _progress(f"collected {len(prs)} open PR(s) across {len(cfg.repos)} repo(s).")
    return prs


def _plan_item(pr: PR, decision: Decision) -> dict[str, Any] | None:
    """The self-contained, replayable action list for one PR, or None when
    there is nothing to write."""
    actions: dict[str, Any] = {}
    if decision.add_to_project:
        actions["add_to_project"] = True
    # Emit the Source backfill whenever it is unset; apply orders add_to_project
    # first and captures the new item id, so a just-added PR can still be set.
    if pr.source_unset:
        actions["set_source"] = pr.source
    if decision.set_status:
        actions["set_status"] = decision.set_status
    if decision.set_assignee:
        actions["set_assignee"] = decision.set_assignee
        actions["request_reviewers"] = list(decision.request_reviewers)
    if decision.remove_reviewers:
        actions["remove_reviewers"] = list(decision.remove_reviewers)
    if not actions:
        return None
    return {
        "pr": pr.key(),
        "repo": pr.repo,
        "number": pr.number,
        "item_id": pr.project_item_id,
        "node_id": pr.node_id,
        **actions,
    }


def run_sweep(gh: Gh, cfg: Config, now: datetime) -> dict[str, Any]:
    """Compute the board-reconciliation plan (side-effect free apart from
    writing the plan file). The report + stall clocks live in pr_report.py."""
    # Resolve the fallback assignee once (override -> maintainer team -> bmillsNV)
    # so _select_assignee_reviewers can use it as the terminal pick.
    cfg.maintainer = resolve_maintainer(gh, cfg)
    owners_members = list_team_members(gh, cfg.owners_team)
    bot_owners_members = list_team_members(gh, cfg.bot_owners_team)

    # The write+ collaborator set is per-repo, memoized, and shared between source
    # classification (collect_prs) and the extra-reviewer pool (selection below).
    collaborators_cache: dict[str, set[str]] = {}

    def repo_collaborators(repo: str) -> set[str]:
        if repo not in collaborators_cache:
            collaborators_cache[repo] = collect_repo_collaborators(gh, repo)
        return collaborators_cache[repo]

    prs = collect_prs(gh, cfg, repo_collaborators)

    repo_stats: dict[str, dict[str, int]] = {}
    plan: list[dict[str, Any]] = []  # self-contained, replayable action list

    for pr in prs:
        stats = repo_stats.setdefault(pr.repo, {"open": 0, "acted": 0})
        stats["open"] += 1

        _select_assignee_reviewers(
            pr, cfg, owners_members, bot_owners_members, repo_collaborators)

        decision = reconcile(pr, cfg)
        if not decision.is_noop() or pr.source_unset:
            stats["acted"] += 1

        item = _plan_item(pr, decision)
        if item is not None:
            plan.append(item)

    summary: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "repos": repo_stats,
        "plan": plan,
    }

    # Planning always writes the plan so an `--apply` pass can replay it.
    save_plan(cfg.plan_file, summary)
    return summary


def apply_plan(gh: Gh, cfg: Config, plan: list[dict[str, Any]]) -> int:
    """Execute a previously computed plan (each item is self-contained). Reuses
    the same idempotent Applier the one-shot path uses. Returns item count."""
    applier = Applier(gh, cfg)
    for item in plan:
        pr = PR(
            repo=item["repo"],
            number=int(item["number"]),
            project_item_id=item.get("item_id"),
            node_id=item.get("node_id") or "",
        )
        if item.get("add_to_project"):
            applier.add_to_project(pr)
        if item.get("set_source"):
            applier.set_source(pr, item["set_source"])
        if item.get("set_status"):
            applier.set_status(pr, item["set_status"])
        if item.get("set_assignee"):
            applier.set_assignee(pr, item["set_assignee"])
            applier.request_reviewers(pr, item.get("request_reviewers") or [])
        if item.get("remove_reviewers"):
            applier.remove_reviewers(pr, item["remove_reviewers"])
    return len(plan)


def apply_decisions(gh: Gh, cfg: Config, doc: dict[str, Any]) -> int:
    """The single apply step shared by every apply path (one-shot and replay):
    execute the plan's GitHub writes. If a plan embeds a `state` blob (legacy /
    externally produced), it is persisted too. Returns the number of plan items
    executed."""
    n = apply_plan(gh, cfg, doc.get("plan") or [])
    state = doc.get("state")
    if isinstance(state, dict):
        save_state(cfg.state_file, state)
    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Reconcile open shader-slang PRs on the 'Slang PR Tracking' board "
            "(ProjectsV2 read+write). All configuration lives in the constants "
            "at the top of pr_common.py. Modes: no flags plans (dry run, writes "
            "the plan file); '--apply' computes and applies in one shot; "
            "'--replay' applies the previously saved plan as-is. The fallback "
            "assignee is resolved from the maintainer team (then bmillsNV); pass "
            "'--maintainer LOGIN' only to override it. The board-free report "
            "lives in pr_report.py."
        )
    )
    p.add_argument("--maintainer", default="",
                   help="Optional override for the fallback assignee. When omitted it is "
                        "resolved from the maintainer team (shader-slang/slang-maintainer), "
                        "else bmillsNV.")
    p.add_argument("--apply", action="store_true",
                   help="Compute a fresh plan and apply it (one-shot). Omit to plan only "
                        "(dry run).")
    p.add_argument("--replay", action="store_true",
                   help="Apply the previously saved plan file as-is, without recomputing.")
    return p.parse_args(argv)


def _print_summary(summary: dict[str, Any], cfg: Config, applying: bool) -> None:
    mode = "ONE-SHOT" if applying else "PLAN (dry run)"
    print(f"[{mode}] sweep @ {summary['generated_at']}")
    for repo, st in sorted(summary["repos"].items()):
        print(f"  {repo}: {st['open']} open, {st['acted']} actioned")
    plan = summary["plan"]
    def _count(key: str) -> int:
        return sum(1 for item in plan if item.get(key))
    print(f"  transitions={_count('set_status')} "
          f"assignments={_count('set_assignee')} "
          f"sources_set={_count('set_source')} "
          f"added_to_board={_count('add_to_project')}")
    print(f"  plan ({len(plan)} items) written to {cfg.plan_file}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    maintainer = args.maintainer.strip().lstrip("@")

    gh = Gh(find_gh())
    gh.preflight()

    # Replay: apply the saved plan as-is, without recomputing. This is the bare
    # "apply half"; the one-shot path below runs the same apply step on a freshly
    # computed plan, so one-shot == plan + replay.
    if args.replay:
        cfg = Config(apply=True)
        doc = load_plan(cfg.plan_file)
        if doc is None:
            raise SystemExit(
                f"No saved plan at {cfg.plan_file}. Run a plan pass first "
                "(no flags), or use --apply to compute + apply in one shot."
            )
        n = apply_decisions(gh, cfg, doc)
        print(f"[APPLY] executed {n} planned items from {cfg.plan_file} "
              f"(planned {doc.get('generated_at')})")
        return 0

    # Plan (no flags, dry run) or one-shot (--apply). The fallback assignee is
    # resolved inside run_sweep (override -> maintainer team -> bmillsNV); the
    # optional --maintainer here is just that override.
    cfg = Config(maintainer=maintainer, apply=args.apply)

    # Default scope: every non-archived repo in the org (DEFAULT_REPOS is empty).
    if not cfg.repos:
        cfg.repos = list_org_repos(gh, cfg.org)
        if not cfg.repos:
            raise SystemExit(f"No repositories found in org {cfg.org!r}.")

    now = datetime.now(timezone.utc)
    summary = run_sweep(gh, cfg, now)        # the "plan half"
    _print_summary(summary, cfg, applying=args.apply)

    if args.apply:                           # one-shot = plan + apply, same step
        n = apply_decisions(gh, cfg, summary)
        print(f"\n[APPLY] executed {n} planned items.")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
