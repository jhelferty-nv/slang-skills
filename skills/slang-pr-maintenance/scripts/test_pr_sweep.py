#!/usr/bin/env python3
"""Unit tests for the state machine (pr_sweep.py) and the shared library
(pr_common.py) + the committer-signal ranking (pr_signal.py).

No live `gh` calls — every test constructs plain data and checks the synthesis
logic (classification, the state machine, working-hours gates, CI summarization,
committer-signal weighting, the hybrid tiebreak bookkeeping, and the apply/plan
round-trip). The board-free report logic is covered by test_pr_report.py.

Run:  python3 scripts/test_pr_sweep.py
"""
from __future__ import annotations

# Tests use unittest's setUp pattern, which confuses strict type inference.
# Relax the strict rules for this test module.
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportImplicitOverride=false, reportUninitializedInstanceVariable=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportUnusedCallResult=false, reportImplicitRelativeImport=false, reportUnusedParameter=false, reportPrivateUsage=false

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from typing import final

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import pr_common as common  # noqa: E402  (path inserted above)
import pr_sweep as sweep    # noqa: E402
import pr_signal as signal  # noqa: E402


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def make_cfg(**kw):
    base = dict(repos=["shader-slang/slang"], project_id="P",
                bot_authors=["nv-slang-bot", "slang-coworker-nanoclaw", "Copilot", "copilot-swe-agent"])
    base.update(kw)
    return common.Config(**base)


def make_pr(**kw):
    # On the board by default (project_item_id set) so lifecycle tests don't trip
    # the off-board rule; Community source by default (the bot-overseen human
    # flow). Pass project_item_id=None / source=... to test other paths.
    defaults = dict(repo="shader-slang/slang", number=1, project_item_id="PVTI_x",
                    source="Community")
    defaults.update(kw)
    return common.PR(**defaults)


@final
class TestClassification(unittest.TestCase):
    def test_bot_author_variants(self):
        bots = ["nv-slang-bot", "slang-coworker-nanoclaw"]
        self.assertTrue(signal.classify_is_bot("nv-slang-bot", bots))
        self.assertTrue(signal.classify_is_bot("nv-slang-bot[bot]", bots))
        self.assertTrue(signal.classify_is_bot("app/nv-slang-bot", bots))
        self.assertTrue(signal.classify_is_bot("slang-coworker-nanoclaw[bot]", bots))
        self.assertFalse(signal.classify_is_bot("some-human", bots))


@final
class TestWorkingHours(unittest.TestCase):
    def setUp(self):
        self.tz = timezone.utc

    def test_weekday_span(self):
        # Mon 09:00 -> Tue 09:00 = 24 working hours
        start = utc(2026, 6, 8, 9)   # Monday
        end = utc(2026, 6, 9, 9)     # Tuesday
        self.assertAlmostEqual(common.working_hours_between(start, end, self.tz), 24.0, places=3)

    def test_weekend_skipped(self):
        # Fri 12:00 -> Mon 12:00 spans a full weekend; only Fri 12:00->Sat 00:00
        # (12h) + Mon 00:00->Mon 12:00 (12h) = 24 working hours.
        start = utc(2026, 6, 12, 12)  # Friday
        end = utc(2026, 6, 15, 12)    # Monday
        self.assertAlmostEqual(common.working_hours_between(start, end, self.tz), 24.0, places=3)

    def test_zero_when_reversed(self):
        self.assertEqual(common.working_hours_between(utc(2026, 6, 9), utc(2026, 6, 8), self.tz), 0.0)


@final
class TestCiSummary(unittest.TestCase):
    def test_action_required_wins(self):
        runs = [{"status": "completed", "conclusion": "success"},
                {"status": "completed", "conclusion": "action_required"}]
        self.assertEqual(common.summarize_ci(runs), common.CI_ACTION_REQUIRED)

    def test_pending(self):
        runs = [{"status": "in_progress", "conclusion": None}]
        self.assertEqual(common.summarize_ci(runs), common.CI_PENDING)

    def test_failed(self):
        runs = [{"status": "completed", "conclusion": "failure"}]
        self.assertEqual(common.summarize_ci(runs), common.CI_FAILED)

    def test_passed(self):
        runs = [{"status": "completed", "conclusion": "success"},
                {"status": "completed", "conclusion": "skipped"}]
        self.assertEqual(common.summarize_ci(runs), common.CI_PASSED)

    def test_none(self):
        self.assertEqual(common.summarize_ci([]), common.CI_NONE)


@final
class TestTargetStatus(unittest.TestCase):
    """The five-state lattice (pr_common.target_status). Must mirror
    computeTarget() in pr-board-sync.yml. Priority: changes-request -> draft ->
    CI-failed -> approved -> In Review."""
    def setUp(self):
        self.cfg = make_cfg()

    def t(self, **kw):
        return common.target_status(make_pr(**kw), self.cfg)

    def test_changes_requested_is_revising(self):
        self.assertEqual(self.t(change_requested=True), "Revising")

    def test_bot_draft_with_changes_requested_is_revising(self):
        self.assertEqual(self.t(is_bot=True, is_draft=True, change_requested=True), "Revising")

    def test_changes_requested_dominates_ci_failure(self):
        self.assertEqual(self.t(change_requested=True, ci_state=common.CI_FAILED), "Revising")

    # (Review-freshness is now decided in summarize_reviews via commit identity;
    # see TestSummarizeReviews. target_status just consumes change_requested/approved.)

    def test_human_draft_is_revising(self):
        self.assertEqual(self.t(is_draft=True, is_bot=False), "Revising")

    def test_bot_draft_is_in_review(self):
        self.assertEqual(self.t(is_draft=True, is_bot=True), "In Review")

    def test_bot_draft_failing_ci_still_in_review(self):
        self.assertEqual(self.t(is_draft=True, is_bot=True, ci_state=common.CI_FAILED), "In Review")

    def test_human_ci_failed_is_snagged(self):
        self.assertEqual(self.t(is_bot=False, ci_state=common.CI_FAILED), "Snagged")

    def test_bot_ci_failed_is_revising(self):
        self.assertEqual(self.t(is_bot=True, ci_state=common.CI_FAILED), "Revising")

    def test_ci_action_required_is_snagged(self):
        # Needs a maintainer to approve the CI run -> Snagged, for bot and human alike.
        self.assertEqual(self.t(is_bot=False, ci_state=common.CI_ACTION_REQUIRED), "Snagged")
        self.assertEqual(self.t(is_bot=True, ci_state=common.CI_ACTION_REQUIRED), "Snagged")

    def test_changes_requested_still_wins_over_action_required(self):
        # changes-requested ranks above the CI tier (the documented edge case).
        self.assertEqual(
            self.t(change_requested=True, ci_state=common.CI_ACTION_REQUIRED), "Revising")

    def test_approved_ci_pending_is_approved(self):
        self.assertEqual(self.t(approved=True, ci_state=common.CI_PENDING), "Approved")

    def test_approved_in_queue_is_approved(self):
        self.assertEqual(
            self.t(approved=True, ci_state=common.CI_PASSED, in_merge_queue=True), "Approved")

    def test_approved_green_unqueued_is_snagged(self):
        self.assertEqual(
            self.t(approved=True, ci_state=common.CI_PASSED, in_merge_queue=False), "Snagged")

    def test_default_is_in_review(self):
        self.assertEqual(self.t(ci_state=common.CI_PENDING), "In Review")


@final
class TestReconcile(unittest.TestCase):
    """reconcile() = terminal-Done / set target_status / assign / add-to-board."""
    def setUp(self):
        self.cfg = make_cfg()

    def test_merged_goes_done(self):
        pr = make_pr(state="MERGED", board_status="In Review")
        self.assertEqual(sweep.reconcile(pr, self.cfg).set_status, "Done")

    def test_merged_already_done_noop(self):
        self.assertTrue(
            sweep.reconcile(make_pr(state="MERGED", board_status="Done"), self.cfg).is_noop())

    def test_sets_target_when_board_differs(self):
        pr = make_pr(author="alice", assignees=["bob"], change_requested=True,
                     board_status="In Review")
        self.assertEqual(sweep.reconcile(pr, self.cfg).set_status, "Revising")

    def test_noop_when_board_matches_target(self):
        pr = make_pr(author="alice", assignees=["bob"], change_requested=True,
                     board_status="Revising")
        self.assertTrue(sweep.reconcile(pr, self.cfg).is_noop())

    def test_human_ci_failed_to_snagged(self):
        pr = make_pr(author="alice", assignees=["bob"], ci_state=common.CI_FAILED,
                     board_status="In Review")
        self.assertEqual(sweep.reconcile(pr, self.cfg).set_status, "Snagged")

    def test_bot_ci_failed_to_revising(self):
        pr = make_pr(author="nv-slang-bot", is_bot=True, source="Bot", assignees=["bob"],
                     ci_state=common.CI_FAILED, board_status="In Review")
        self.assertEqual(sweep.reconcile(pr, self.cfg).set_status, "Revising")

    def test_approved_green_unqueued_to_snagged(self):
        pr = make_pr(author="alice", assignees=["bob"], approved=True,
                     ci_state=common.CI_PASSED, in_merge_queue=False, board_status="Approved")
        self.assertEqual(sweep.reconcile(pr, self.cfg).set_status, "Snagged")

    def test_approved_in_queue_to_approved(self):
        pr = make_pr(author="alice", assignees=["bob"], approved=True,
                     ci_state=common.CI_PASSED, in_merge_queue=True, board_status="In Review")
        self.assertEqual(sweep.reconcile(pr, self.cfg).set_status, "Approved")

    # --- assignment --------------------------------------------------------
    def test_unassigned_human_gets_assignee_and_reviewers(self):
        pr = make_pr(author="alice", assignee_pick="bob", review_requests=["bob", "carol"],
                     ci_state=common.CI_PENDING, board_status="In Review")
        d = sweep.reconcile(pr, self.cfg)
        self.assertEqual(d.set_assignee, "bob")
        self.assertEqual(d.request_reviewers, ["bob", "carol"])

    def test_human_draft_exempt_from_assignment(self):
        pr = make_pr(author="alice", is_draft=True, board_status="In Review",
                     assignee_pick="bob")
        d = sweep.reconcile(pr, self.cfg)
        self.assertEqual(d.set_status, "Revising")
        self.assertIsNone(d.set_assignee)

    def test_bot_draft_shepherded_in_review_with_owner(self):
        pr = make_pr(author="nv-slang-bot", is_bot=True, source="Bot", is_draft=True,
                     board_status=None, assignee_pick="bob")
        d = sweep.reconcile(pr, self.cfg)
        self.assertEqual(d.set_status, "In Review")
        self.assertEqual(d.set_assignee, "bob")

    def test_merge_queued_left_unassigned(self):
        pr = make_pr(author="alice", approved=True, ci_state=common.CI_PASSED,
                     in_merge_queue=True, board_status="Approved", assignee_pick=None)
        self.assertTrue(sweep.reconcile(pr, self.cfg).is_noop())

    # --- off-board (add + set fields in one pass) -------------------------
    def test_off_board_pr_added(self):
        d = sweep.reconcile(make_pr(author="alice", project_item_id=None), self.cfg)
        self.assertTrue(d.add_to_project)
        self.assertFalse(d.is_noop())

    def test_off_board_pr_added_and_set_same_pass(self):
        # A new (off-board) PR is added AND gets its initial status in one pass.
        pr = make_pr(author="alice", project_item_id=None, assignees=["bob"],
                     approved=True, ci_state=common.CI_PASSED, in_merge_queue=True,
                     board_status=None)
        d = sweep.reconcile(pr, self.cfg)
        self.assertTrue(d.add_to_project)
        self.assertEqual(d.set_status, "Approved")

    def test_off_board_plan_item_carries_source(self):
        # The plan for an off-board PR includes the Source backfill even though it
        # has no project_item_id yet (apply captures the new id first).
        pr = make_pr(author="alice", project_item_id=None, source="Internal",
                     approved=True, ci_state=common.CI_PASSED, in_merge_queue=True,
                     assignees=["bob"], board_status=None)
        pr.source_unset = True
        item = sweep._plan_item(pr, sweep.reconcile(pr, self.cfg))
        assert item is not None
        self.assertTrue(item["add_to_project"])
        self.assertEqual(item["set_source"], "Internal")


@final
class TestApplyDecisions(unittest.TestCase):
    def test_plan_roundtrips_state(self):
        summary = {"plan": [{"repo": "o/r", "number": 1, "set_status": "In Review"}],
                   "digest": "d", "state": {"prs": {"o/r#1": {"notified": {"idle": "T"}}}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.json")
            sweep.save_plan(path, summary)
            doc = sweep.load_plan(path)
        assert doc is not None
        self.assertEqual(doc["state"], summary["state"])
        self.assertEqual(doc["plan"], summary["plan"])

    def test_apply_decisions_persists_state_and_replays(self):
        doc = {"plan": [{"repo": "o/r", "number": 1, "item_id": "I", "set_status": "In Review"},
                        {"repo": "o/r", "number": 2, "item_id": "J", "set_assignee": "bob",
                         "request_reviewers": [], "remove_reviewers": ["bmillsNV"]}],
               "state": {"prs": {"o/r#1": {"notified": {"idle": "T"}}}}}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            cfg = make_cfg(state_file=state_path)
            gh = _ApplyGh()
            n = sweep.apply_decisions(gh, cfg, doc)
            self.assertEqual(n, 2)  # both plan items executed
            self.assertTrue(gh.run_calls > 0)
            with open(state_path) as f:
                self.assertEqual(json.load(f), doc["state"])  # state persisted

    def test_add_to_project_captures_new_item_id(self):
        # add_to_project must store the freshly created item id on the PR so the
        # same apply pass can set its project fields.
        applier = sweep.Applier(_ApplyGh(), make_cfg())
        pr = common.PR(repo="o/r", number=1, node_id="PR_node", project_item_id=None)
        applier.add_to_project(pr)
        self.assertEqual(pr.project_item_id, "NEW_ITEM")

    def test_apply_decisions_no_state_writes_no_file(self):
        doc = {"plan": [{"repo": "o/r", "number": 1, "item_id": "I", "set_status": "In Review"}]}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            cfg = make_cfg(state_file=state_path)
            sweep.apply_decisions(_ApplyGh(), cfg, doc)
            self.assertFalse(os.path.exists(state_path))  # nothing to persist


@final
class TestSelectAssigneeAndReviewers(unittest.TestCase):
    OWNERS = {"owner1", "owner2"}
    COLLAB = {"owner1", "owner2", "dev1", "dev2"}  # owners are also collaborators
    MAINT = "maintainer"

    def select(self, issue, committers, author="author"):
        return common.select_assignee_and_reviewers(
            issue, committers, self.OWNERS, self.COLLAB, author, self.MAINT)

    def test_issue_assignee_wins(self):
        # issue assigned to owner2; commit signal favors owner1 -> issue wins
        assignee, reviewers = self.select(["owner2"], ["owner1", "dev1"])
        self.assertEqual(assignee, "owner2")
        self.assertIn("owner2", reviewers)

    def test_commit_signal_owner_when_no_issue(self):
        assignee, _reviewers = self.select([], ["owner1", "owner2"])
        self.assertEqual(assignee, "owner1")  # most recent owner committer

    def test_maintainer_fallback(self):
        assignee, reviewers = self.select([], ["dev1"])  # no owner committed
        self.assertEqual(assignee, "maintainer")
        self.assertEqual(reviewers, ["maintainer", "dev1"])  # top collaborator-not-owner added

    def test_extra_reviewer_is_top_collaborator_not_owner(self):
        # committers newest-first: dev2, owner1, dev1 -> assignee owner1, extra dev2
        assignee, reviewers = self.select([], ["dev2", "owner1", "dev1"])
        self.assertEqual(assignee, "owner1")
        self.assertEqual(reviewers, ["owner1", "dev2"])

    def test_no_collaborator_committer_means_only_assignee(self):
        _assignee, reviewers = self.select([], ["owner1", "owner2"])
        self.assertEqual(reviewers, ["owner1"])

    def test_author_never_requested_as_reviewer(self):
        # maintainer happens to equal author -> dropped from reviewers
        assignee, reviewers = common.select_assignee_and_reviewers(
            [], ["dev1"], self.OWNERS, self.COLLAB, author="maintainer", maintainer="maintainer")
        self.assertEqual(assignee, "maintainer")
        self.assertEqual(reviewers, ["dev1"])  # author(maintainer) excluded from reviewers

    def test_real_existing_reviewer_blocks_adding(self):
        # A real (approve-capable) reviewer already requested -> add no reviewers.
        assignee, reviewers = common.select_assignee_and_reviewers(
            [], ["dev2", "owner1"], self.OWNERS, self.COLLAB, "author", self.MAINT,
            existing_reviewers=["dave"], ignored_reviewers={"bmillsNV"})
        self.assertEqual(assignee, "owner1")
        self.assertEqual(reviewers, [])

    def test_ignored_and_bot_reviewers_do_not_count_as_existing(self):
        # bmillsNV (ignored, can't approve) + a [bot] don't block adding reviewers.
        assignee, reviewers = common.select_assignee_and_reviewers(
            [], ["dev2", "owner1"], self.OWNERS, self.COLLAB, "author", self.MAINT,
            existing_reviewers=["bmillsNV", "copilot[bot]"],
            bot_authors=["nv-slang-bot"], ignored_reviewers={"bmillsNV"})
        self.assertEqual(assignee, "owner1")
        self.assertEqual(reviewers, ["owner1", "dev2"])


@final
class TestSourceClassify(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_bot(self):
        self.assertEqual(common.source_for(True, False, self.cfg), "Bot")

    def test_internal_when_can_commit(self):
        self.assertEqual(common.source_for(False, True, self.cfg), "Internal")

    def test_community_when_cannot_commit(self):
        self.assertEqual(common.source_for(False, False, self.cfg), "Community")


@final
class TestWeightedSignal(unittest.TestCase):
    TABLE = [("source/slang/**", 3.0), ("source/**", 2.0), ("tests/**", 1.0)]

    def test_multiplier_most_specific_wins(self):
        self.assertEqual(signal.match_file_multiplier("source/slang/x.cpp", self.TABLE, 1.0), 3.0)
        self.assertEqual(signal.match_file_multiplier("source/core/x.cpp", self.TABLE, 1.0), 2.0)
        self.assertEqual(signal.match_file_multiplier("tests/x.slang", self.TABLE, 1.0), 1.0)
        self.assertEqual(signal.match_file_multiplier("README.md", self.TABLE, 1.0), 1.0)  # default

    def test_per_file_signals(self):
        loc = {"source/slang/a.cpp": 10.0, "tests/b.slang": 10.0}
        sig = signal.per_file_signals(loc, self.TABLE, 1.0)
        self.assertEqual(sig, {"source/slang/a.cpp": 30.0, "tests/b.slang": 10.0})

    def test_top_k_weights_normalizes(self):
        weights = signal.top_k_weights({"a": 30.0, "b": 10.0}, k=10)
        self.assertAlmostEqual(weights["a"], 0.75)
        self.assertAlmostEqual(weights["b"], 0.25)

    def test_top_k_limits(self):
        weights = signal.top_k_weights({"a": 3.0, "b": 2.0, "c": 1.0}, k=2)
        self.assertEqual(set(weights), {"a", "b"})  # c dropped

    def test_top_k_empty_when_no_signal(self):
        self.assertEqual(signal.top_k_weights({"a": 0.0}, k=5), {})

    def test_overall_signal_sums_weight_times_loc(self):
        weights = {"f1": 0.75, "f2": 0.25}
        loc = {"f1": {"alice": 40.0, "bob": 10.0}, "f2": {"bob": 20.0}}
        overall = signal.overall_signal(weights, loc)
        self.assertAlmostEqual(overall["alice"], 30.0)          # 0.75*40
        self.assertAlmostEqual(overall["bob"], 0.75 * 10 + 0.25 * 20)  # 12.5

    def test_rank_logins_descending(self):
        self.assertEqual(signal.rank_logins({"alice": 30.0, "bob": 12.5}), ["alice", "bob"])

    def test_is_bot_login(self):
        bots = ["nv-slang-bot"]
        self.assertTrue(signal.is_bot_login("github-actions[bot]", bots))
        self.assertTrue(signal.is_bot_login("nv-slang-bot", bots))
        self.assertTrue(signal.is_bot_login("", bots))
        self.assertFalse(signal.is_bot_login("alice", bots))


@final
class TestAttributeCommit(unittest.TestCase):
    BOTS = ["nv-slang-bot"]

    def test_human_author_credited(self):
        self.assertEqual(
            signal.attribute_commit("alice", None, "author", self.BOTS), "alice")

    def test_author_self_uses_approver(self):
        # the PR author's own past commit credits whoever reviewed it
        self.assertEqual(
            signal.attribute_commit("author", "carol", "author", self.BOTS), "carol")

    def test_author_self_no_approver_dropped(self):
        self.assertIsNone(
            signal.attribute_commit("author", None, "author", self.BOTS))

    def test_author_self_approver_is_author_dropped(self):
        self.assertIsNone(
            signal.attribute_commit("author", "author", "author", self.BOTS))

    def test_bot_author_uses_approver(self):
        self.assertEqual(
            signal.attribute_commit("nv-slang-bot", "carol", "author", self.BOTS), "carol")

    def test_unmapped_author_uses_approver(self):
        self.assertEqual(
            signal.attribute_commit("", "carol", "author", self.BOTS), "carol")

    def test_bot_author_no_approver_dropped(self):
        self.assertIsNone(
            signal.attribute_commit("nv-slang-bot", None, "author", self.BOTS))

    def test_bot_author_approver_is_pr_author_dropped(self):
        self.assertIsNone(
            signal.attribute_commit("nv-slang-bot", "author", "author", self.BOTS))

    def test_bot_author_approver_is_bot_dropped(self):
        self.assertIsNone(
            signal.attribute_commit("nv-slang-bot", "nv-slang-bot", "author", self.BOTS))


@final
class TestHybridTiebreak(unittest.TestCase):
    def test_needs_tiebreak_close(self):
        self.assertTrue(signal.needs_tiebreak({"a": 10.0, "b": 8.0}, ["a", "b"], 1.5))

    def test_no_tiebreak_clear_winner(self):
        self.assertFalse(signal.needs_tiebreak({"a": 20.0, "b": 8.0}, ["a", "b"], 1.5))

    def test_no_tiebreak_single_candidate(self):
        self.assertFalse(signal.needs_tiebreak({"a": 5.0}, ["a"], 1.5))

    def test_no_tiebreak_second_zero(self):
        self.assertFalse(signal.needs_tiebreak({"a": 5.0, "b": 0.0}, ["a", "b"], 1.5))

    def test_merge_refined_reorders_top_n_keeps_tail(self):
        # refined per-file score flips a and b within the top-2 finalists
        self.assertEqual(
            signal.merge_refined(["a", "b", "c", "d"], 2, {"a": 1.0, "b": 5.0}),
            ["b", "a", "c", "d"])

    def test_merge_refined_all_finalists(self):
        self.assertEqual(
            signal.merge_refined(["a", "b"], 2, {"a": 1.0, "b": 2.0}), ["b", "a"])


@final
class _CountingGh:
    """Minimal fake gh that records graphql/api call counts for cache tests."""
    def __init__(self):
        self.graphql_calls = 0
        self.api_calls = 0

    def graphql(self, query, variables=None):
        self.graphql_calls += 1
        # One history node for whichever file aliases the query requested.
        target = {}
        i = 0
        while ("f%d:" % i) in query or ("f%d :" % i) in query:
            i += 1
        for j in range(max(i, 1)):
            target["f%d" % j] = {"nodes": [{
                "oid": "sha%d" % j, "additions": 10, "deletions": 2,
                "author": {"user": {"login": "alice"}},
                "associatedPullRequests": {"nodes": []},
            }]}
        return {"data": {"repository": {"defaultBranchRef": {"target": target}}}}

    def api(self, path, jq=None, paginate=False):
        self.api_calls += 1
        return '{"filename": "a.cpp", "additions": 5, "deletions": 1}'


@final
class TestSignalCache(unittest.TestCase):
    def test_file_history_cached_across_calls(self):
        gh = _CountingGh()
        cache = {}
        signal.fetch_file_histories(gh, "o/r", ["a.cpp", "b.cpp"], "S", 5, cache)
        self.assertEqual(gh.graphql_calls, 1)
        # Same paths again -> fully cached -> no new query.
        signal.fetch_file_histories(gh, "o/r", ["a.cpp", "b.cpp"], "S", 5, cache)
        self.assertEqual(gh.graphql_calls, 1)
        # A new path -> one more query (only the uncached path).
        signal.fetch_file_histories(gh, "o/r", ["a.cpp", "c.cpp"], "S", 5, cache)
        self.assertEqual(gh.graphql_calls, 2)

    def test_file_history_keyed_by_repo(self):
        gh = _CountingGh()
        cache = {}
        signal.fetch_file_histories(gh, "o/r1", ["a.cpp"], "S", 5, cache)
        signal.fetch_file_histories(gh, "o/r2", ["a.cpp"], "S", 5, cache)
        self.assertEqual(gh.graphql_calls, 2)  # same path, different repo -> not shared

    def test_commit_file_loc_cached_by_repo_sha(self):
        gh = _CountingGh()
        cache = {}
        signal.commit_file_loc(gh, "o/r", "deadbeef", "a.cpp", cache)
        signal.commit_file_loc(gh, "o/r", "deadbeef", "a.cpp", cache)  # cached
        self.assertEqual(gh.api_calls, 1)
        signal.commit_file_loc(gh, "o/r", "cafef00d", "a.cpp", cache)  # new sha
        self.assertEqual(gh.api_calls, 2)


@final
class _ApplyGh:
    """No-op fake gh for the apply path: records the writes attempted."""
    def __init__(self):
        self.run_calls = 0
        self.graphql_calls = 0

    def run(self, args, check=True):
        self.run_calls += 1
        return ""

    def graphql(self, query, variables=None):
        self.graphql_calls += 1
        if "addProjectV2ItemById" in query:
            return {"data": {"addProjectV2ItemById": {"item": {"id": "NEW_ITEM"}}}}
        return {}

    def api(self, path, jq=None, paginate=False):
        return ""


@final
class TestSummarizeReviews(unittest.TestCase):
    """Per-reviewer latest opinion, counted only when made on the CURRENT head
    commit (commit.oid == head_oid)."""
    HEAD = "headsha"

    def rv(self, state, login, oid, at):
        return {"state": state, "submittedAt": at, "author": {"login": login},
                "commit": {"oid": oid}}

    def test_empty(self):
        self.assertEqual(common.summarize_reviews([], self.HEAD), (None, False, False))

    def test_changes_requested_on_head(self):
        last, changed, approved = common.summarize_reviews(
            [self.rv("CHANGES_REQUESTED", "alice", self.HEAD, "2026-06-02T00:00:00Z")], self.HEAD)
        self.assertEqual(last, utc(2026, 6, 2))
        self.assertTrue(changed)
        self.assertFalse(approved)

    def test_approved_on_head_case_insensitive(self):
        _l, changed, approved = common.summarize_reviews(
            [self.rv("approved", "alice", self.HEAD, "2026-06-02T00:00:00Z")], self.HEAD)
        self.assertFalse(changed)
        self.assertTrue(approved)

    def test_changes_request_on_old_commit_is_stale(self):
        # opinion made on an earlier commit -> superseded by the new head -> ignored
        _l, changed, approved = common.summarize_reviews(
            [self.rv("CHANGES_REQUESTED", "alice", "oldsha", "2026-06-02T00:00:00Z")], self.HEAD)
        self.assertFalse(changed)
        self.assertFalse(approved)

    def test_approval_on_old_commit_is_stale(self):
        _l, changed, approved = common.summarize_reviews(
            [self.rv("APPROVED", "alice", "oldsha", "2026-06-02T00:00:00Z")], self.HEAD)
        self.assertFalse(approved)
        self.assertFalse(changed)

    def test_same_reviewer_latest_on_head_wins(self):
        # alice requested changes on an old commit, then approved the head
        reviews = [
            self.rv("CHANGES_REQUESTED", "alice", "oldsha", "2026-06-01T00:00:00Z"),
            self.rv("APPROVED", "alice", self.HEAD, "2026-06-02T00:00:00Z"),
        ]
        _l, changed, approved = common.summarize_reviews(reviews, self.HEAD)
        self.assertFalse(changed)
        self.assertTrue(approved)

    def test_outstanding_change_request_blocks_approved(self):
        # both on head: alice approved, bob requests changes
        reviews = [
            self.rv("APPROVED", "alice", self.HEAD, "2026-06-01T00:00:00Z"),
            self.rv("CHANGES_REQUESTED", "bob", self.HEAD, "2026-06-02T00:00:00Z"),
        ]
        _l, changed, approved = common.summarize_reviews(reviews, self.HEAD)
        self.assertTrue(changed)
        self.assertFalse(approved)

    def test_last_review_at_includes_nondecisive(self):
        reviews = [
            self.rv("APPROVED", "alice", self.HEAD, "2026-06-01T00:00:00Z"),
            self.rv("COMMENTED", "bob", self.HEAD, "2026-06-03T00:00:00Z"),
        ]
        last, _c, approved = common.summarize_reviews(reviews, self.HEAD)
        self.assertEqual(last, utc(2026, 6, 3))  # latest of any type (stall signal)
        self.assertTrue(approved)

    def test_no_head_oid_counts_all_latest_opinions(self):
        # fallback when head_oid is unknown ("") -> currency not enforced
        _l, _c, approved = common.summarize_reviews(
            [self.rv("APPROVED", "alice", "whatever", "2026-06-01T00:00:00Z")])
        self.assertTrue(approved)


@final
class TestCIFromRollup(unittest.TestCase):
    def test_null_rollup(self):
        self.assertEqual(common.ci_state_from_rollup(None), common.CI_NONE)

    def test_action_required(self):
        rollup = {"contexts": {"nodes": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "ACTION_REQUIRED"}]}}
        self.assertEqual(common.ci_state_from_rollup(rollup), common.CI_ACTION_REQUIRED)

    def test_pending_failed_passed(self):
        pend = {"contexts": {"nodes": [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}]}}
        self.assertEqual(common.ci_state_from_rollup(pend), common.CI_PENDING)
        fail = {"contexts": {"nodes": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]}}
        self.assertEqual(common.ci_state_from_rollup(fail), common.CI_FAILED)
        ok = {"contexts": {"nodes": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]}}
        self.assertEqual(common.ci_state_from_rollup(ok), common.CI_PASSED)

    def test_legacy_status_context(self):
        rollup = {"contexts": {"nodes": [{"__typename": "StatusContext", "context": "ci", "state": "FAILURE"}]}}
        self.assertEqual(common.ci_state_from_rollup(rollup), common.CI_FAILED)


@final
class TestParsePrNode(unittest.TestCase):
    def _node(self, **over):
        node = {
            "number": 42, "title": "t", "url": "u", "id": "PR_x", "isDraft": False,
            "headRefOid": "abc123", "createdAt": "2026-06-01T00:00:00Z",
            "updatedAt": "2026-06-10T00:00:00Z", "reviewDecision": "REVIEW_REQUIRED",
            "author": {"login": "alice", "__typename": "User"},
            "assignees": {"nodes": [{"login": "bob"}]},
            "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "User", "login": "bmillsNV"}},
                                         {"requestedReviewer": {"__typename": "Team"}}]},
            "commits": {"nodes": [{"commit": {
                "statusCheckRollup": {"contexts": {"nodes": [
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]}}}}]},
            # changes-request on the current head (commit.oid == headRefOid) -> current
            "reviews": {"nodes": [{"state": "CHANGES_REQUESTED", "submittedAt": "2026-06-09T00:00:00Z",
                                   "author": {"login": "rev1"}, "commit": {"oid": "abc123"}}]},
            "mergeQueueEntry": None,
            "closingIssuesReferences": {"nodes": [{"assignees": {"nodes": [{"login": "carol"}]}}]},
            "files": {"nodes": [{"path": "a.cpp", "additions": 10, "deletions": 2}]},
        }
        node.update(over)
        return node

    def test_full_node(self):
        pr = common.parse_pr_node(self._node(), "shader-slang/slang", make_cfg())
        self.assertEqual(pr.number, 42)
        self.assertEqual(pr.author, "alice")
        self.assertFalse(pr.is_bot)
        self.assertEqual(pr.assignees, ["bob"])
        self.assertEqual(pr.existing_reviewers, ["bmillsNV"])  # team entry skipped
        self.assertEqual(pr.ci_state, common.CI_PASSED)
        self.assertTrue(pr.change_requested)  # review is on the head commit (abc123)
        self.assertFalse(pr.approved)
        self.assertEqual(pr.last_review_at, utc(2026, 6, 9))
        self.assertFalse(pr.in_merge_queue)
        self.assertEqual(pr.issue_assignees, ["carol"])
        self.assertEqual(pr.changed_files, {"a.cpp": 10.0})

    def test_bot_author(self):
        pr = common.parse_pr_node(
            self._node(author={"login": "nv-slang-bot", "__typename": "Bot"}),
            "shader-slang/slang", make_cfg())
        self.assertTrue(pr.is_bot)

    def test_merge_queue_present(self):
        pr = common.parse_pr_node(self._node(mergeQueueEntry={"id": "MQ_1"}),
                                  "shader-slang/slang", make_cfg())
        self.assertTrue(pr.in_merge_queue)


if __name__ == "__main__":
    unittest.main()
