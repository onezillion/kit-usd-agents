"""Deterministic tests for identity-safe experiment finish and the operator-recovery wrapper.

The store is exercised on an isolated temporary root. The wrapper (bash + python heredoc)
is exercised at the shell entry level for usage failures and at the MCP-facing level by
asserting its exact exit code against a real ExperimentStore-backed refusal is not
possible without a live MCP daemon; instead the *same semantics* are proven directly
against ExperimentStore.finish with expected experiment IDs, and the wrapper's usage
invariants are proven by invoking its argument validation paths.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "mcp/khl_kit_lab_mcp/src"
sys.path.insert(0, str(SRC))

from khl_kit_lab_mcp.experiments import (  # noqa: E402
    ExperimentConflictError,
    ExperimentStore,
)


WRAPPER = (REPO_ROOT / "mcp/khl_kit_lab_mcp/experiment-user-local.sh").resolve()


class StoreFinishIdentityTests(unittest.TestCase):
    """Identity-safe finish contract on a temporary store root."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = ExperimentStore(self.tempdir.name)

    def _new_active(self):
        manifest = self.store.start(
            title="identity-test",
            objective="Prove identity-safe finish works",
            tags=["identity", "test"],
            runtime_snapshot={"ok": True},
        )
        return manifest["experiment_id"]

    def _files_snapshot(self, experiment_id):
        directory = Path(self.tempdir.name) / experiment_id
        return {
            "manifest": (directory / "manifest.json").read_text(),
            "events": (directory / "events.jsonl").read_text(),
            "summary": (directory / "summary.md").read_text(),
            "current": (Path(self.tempdir.name) / "current.json").read_text(),
        }

    def test_wrong_expected_id_refuses_without_persistence_mutation(self):
        experiment_id = self._new_active()
        before = self._files_snapshot(experiment_id)
        with self.assertRaises(ExperimentConflictError):
            self.store.finish("summary", outcome="success", expected_experiment_id="WRONG-ID")
        after = self._files_snapshot(experiment_id)
        # No persistence mutation anywhere: summary.md, events.jsonl, manifest.json,
        # and current.json all identical before/after.
        self.assertEqual(before, after)
        # Active experiment still active.
        self.assertEqual(self.store.current_id(), experiment_id)

    def test_wrong_expected_id_cannot_finish_active_experiment(self):
        experiment_id = self._new_active()
        with self.assertRaises(ExperimentConflictError) as ctx:
            self.store.finish("x", outcome="failed", expected_experiment_id="other-id")
        message = str(ctx.exception)
        self.assertIn("does not match active experiment", message)
        self.assertEqual(self.store.current_id(), experiment_id)

    def test_right_expected_id_finishes_and_clears_pointer(self):
        experiment_id = self._new_active()
        result = self.store.finish("done", outcome="success", expected_experiment_id=experiment_id)
        self.assertFalse(result["active"])
        self.assertEqual(result["experiment"]["status"], "finished")
        self.assertEqual(result["experiment"]["outcome"], "success")
        self.assertEqual(result["experiment"]["experiment_id"], experiment_id)
        self.assertIsNone(self.store.current_id())
        # summary.md now exists with the title header.
        summary_md = (Path(self.tempdir.name) / experiment_id / "summary.md").read_text()
        self.assertIn("identity-test", summary_md)
        self.assertIn("`success`", summary_md)

    def test_omitted_expected_id_preserves_legacy_compatibility(self):
        experiment_id = self._new_active()
        result = self.store.finish("legacy", outcome="inconclusive")  # no expected id
        self.assertFalse(result["active"])
        self.assertEqual(result["experiment"]["experiment_id"], experiment_id)

    def test_finish_without_active_experiment_refuses(self):
        with self.assertRaises(ExperimentConflictError):
            self.store.finish("x", outcome="success")

    def test_expected_id_format_is_not_enforced_by_store(self):
        # The store compares expected ID to the actual ID; any non-matching value
        # refuses without mutation. Format validation is the caller's responsibility.
        experiment_id = self._new_active()
        with self.assertRaises(ExperimentConflictError):
            self.store.finish("x", outcome="success", expected_experiment_id="not-a-real-format")
        self.assertEqual(self.store.current_id(), experiment_id)


class WrapperUsageTests(unittest.TestCase):
    """Entry-level invariants of the operator-recovery wrapper (no live MCP needed)."""

    def _run(self, *argv):
        return subprocess.run(
            ["bash", str(WRAPPER), *argv],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_no_arguments_fails(self):
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr.lower())

    def test_unknown_subcommand_fails(self):
        result = self._run("nonsense")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown subcommand", result.stderr)

    def test_get_without_id_fails(self):
        result = self._run("get")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr.lower())

    def test_finish_without_expect_id_fails(self):
        result = self._run("finish", "ok", "some summary")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expect-id", result.stderr)

    def test_finish_with_bad_outcome_fails_before_contact(self):
        result = self._run(
            "finish", "--expect-id", "20260101T000000000000Z-x-00000000", "not-an-outcome", "s"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_finish_missing_summary_fails(self):
        result = self._run("finish", "--expect-id", "X", "success", " ")
        self.assertNotEqual(result.returncode, 0)

    def test_finish_with_oversized_summary_fails_before_contact(self):
        result = self._run("finish", "--expect-id", "X", "success", "x" * 20_001)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("summary exceeds", result.stderr)

    def test_get_with_oversized_id_fails_before_contact(self):
        result = self._run("get", "x" * 97)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("experiment ID exceeds", result.stderr)

    def test_current_with_extra_arg_fails(self):
        result = self._run("current", "extra")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr.lower())

    def test_get_with_extra_arg_fails(self):
        result = self._run("get", "a", "b")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr.lower())

    def test_start_with_wrong_arg_count_fails(self):
        for argv in (("start", "t", "o"), ("start", "t", "o", "tag1", "extra")):
            with self.subTest(argv=argv):
                result = self._run(*argv)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("usage", result.stderr.lower())

    def test_finish_with_extra_arg_fails(self):
        result = self._run(
            "finish", "--expect-id", "X", "success", "s", "surplus"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr.lower())

    def test_start_with_empty_title_or_objective_fails(self):
        for argv in (("start", " ", "o", "t"), ("start", "t", "", "t")):
            with self.subTest(argv=argv):
                result = self._run(*argv)
                self.assertNotEqual(result.returncode, 0)

    def test_start_with_oversized_title_fails(self):
        result = self._run("start", "x" * 201, "objective", "t")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr.lower())

    def test_start_with_too_many_tags_fails(self):
        tags = ",".join(f"tag{i}" for i in range(21))
        result = self._run("start", "t", "o", tags)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tags", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
