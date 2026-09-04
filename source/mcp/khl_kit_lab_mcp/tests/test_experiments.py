import asyncio
import json
import re
import stat
import tempfile
import unittest
from pathlib import Path

from khl_kit_lab_mcp.experiments import (
    ExperimentConflictError,
    ExperimentCorruptError,
    ExperimentNotFoundError,
    ExperimentStore,
    ExperimentValidationError,
    generate_experiment_id,
    invoke_and_record,
    validate_experiment_id,
)


class ExperimentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "experiments"
        self.store = ExperimentStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start(self, title: str = "Phase 2C test") -> dict:
        return self.store.start(
            title,
            "Verify durable experiment persistence.",
            ["unit", "phase-2c"],
            {"status": {"ok": True}},
        )

    def test_generated_id_validation(self) -> None:
        experiment_id = generate_experiment_id("Rain / Warp Test")
        self.assertEqual(validate_experiment_id(experiment_id), experiment_id)
        self.assertIn("rain-warp-test", experiment_id)
        for invalid in ("../escape", "/absolute", "bad/id", "not-an-id", ""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ExperimentValidationError):
                    validate_experiment_id(invalid)

    def test_path_containment_rejects_symlink_escape(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        experiment_id = generate_experiment_id("escape")
        (self.root / experiment_id).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ExperimentValidationError):
            self.store.get(experiment_id)

    def test_manifest_symlink_is_rejected(self) -> None:
        manifest = self.start()
        directory = self.root / manifest["experiment_id"]
        real_manifest = directory / "manifest-real.json"
        (directory / "manifest.json").rename(real_manifest)
        (directory / "manifest.json").symlink_to(real_manifest.name)
        with self.assertRaises(ExperimentCorruptError):
            self.store.get(manifest["experiment_id"])

    def test_start_creates_private_layout_and_manifest(self) -> None:
        manifest = self.start()
        directory = self.root / manifest["experiment_id"]
        self.assertEqual(manifest["status"], "active")
        self.assertEqual(manifest["next_sequence"], 2)
        for name in ("manifest.json", "events.jsonl", "summary.md"):
            self.assertTrue((directory / name).is_file())
            self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)
        for name in ("scripts", "results", "artifacts"):
            self.assertTrue((directory / name).is_dir())
            self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o700)

    def test_one_active_experiment_and_atomic_pointer(self) -> None:
        manifest = self.start()
        pointer = self.root / "current.json"
        self.assertEqual(
            json.loads(pointer.read_text())["experiment_id"], manifest["experiment_id"]
        )
        self.assertEqual(stat.S_IMODE(pointer.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.glob(".current.json.*.tmp")), [])
        with self.assertRaises(ExperimentConflictError):
            self.start("second")

    def test_sequence_allocation_result_and_python_source(self) -> None:
        experiment_id = self.start()["experiment_id"]
        first = self.store.record_operation(
            experiment_id,
            "kit_stage_summary",
            {},
            success=True,
            elapsed_ms=1.25,
            result={"ok": True, "prims": 3},
        )
        second = self.store.record_operation(
            experiment_id,
            "kit_execute_python",
            {"code": "2 + 2"},
            success=True,
            elapsed_ms=2.5,
            result={"ok": True, "result": 4},
            python_source="2 + 2",
        )
        self.assertEqual((first["sequence"], second["sequence"]), (2, 3))
        directory = self.root / experiment_id
        self.assertTrue((directory / first["result_file"]).is_file())
        self.assertEqual((directory / second["python_source_file"]).read_text(), "2 + 2")
        result = json.loads((directory / second["result_file"]).read_text())
        self.assertEqual(result["result"]["result"], 4)

    def test_note_and_bounded_get(self) -> None:
        experiment_id = self.start()["experiment_id"]
        for index in range(5):
            self.store.note(f"Observation {index}")
        retrieved = self.store.get(experiment_id, event_limit=2)
        self.assertEqual(len(retrieved["events"]), 2)
        self.assertEqual(retrieved["events"][-1]["note"], "Observation 4")

    def test_automatic_invocation_recording_and_failure_classification(self) -> None:
        experiment_id = self.start()["experiment_id"]

        async def successful() -> dict:
            return {"ok": True, "value": 4}

        result = asyncio.run(
            invoke_and_record(
                self.store,
                experiment_id,
                "kit_stage_summary",
                {},
                successful,
            )
        )
        self.assertEqual(result["value"], 4)

        async def failing() -> dict:
            raise RuntimeError("controlled failure")

        with self.assertRaisesRegex(RuntimeError, "controlled failure"):
            asyncio.run(
                invoke_and_record(
                    self.store,
                    experiment_id,
                    "kit_setting_get",
                    {"path": "/test"},
                    failing,
                )
            )
        events = self.store.get(experiment_id, 10)["events"]
        self.assertTrue(events[-2]["success"])
        self.assertFalse(events[-1]["success"])
        self.assertEqual(events[-1]["result_class"], "transport_or_server_failure")

    def test_python_exception_is_a_legitimate_recorded_result(self) -> None:
        experiment_id = self.start()["experiment_id"]

        async def python_exception() -> dict:
            return {"ok": False, "error": {"code": "PYTHON_EXCEPTION"}}

        asyncio.run(
            invoke_and_record(
                self.store,
                experiment_id,
                "kit_execute_python",
                {"code": "raise RuntimeError()"},
                python_exception,
                python_source="raise RuntimeError()",
            )
        )
        event = self.store.get(experiment_id, 1)["events"][0]
        self.assertTrue(event["success"])
        self.assertEqual(event["result_class"], "python_exception_result")

    def test_finish_writes_summary_and_clears_pointer(self) -> None:
        experiment_id = self.start()["experiment_id"]
        finished = self.store.finish("The persistence check passed.", "success")
        self.assertFalse(finished["active"])
        self.assertEqual(finished["experiment"]["status"], "finished")
        self.assertFalse((self.root / "current.json").exists())
        self.assertFalse(self.store.current()["active"])
        summary = (self.root / experiment_id / "summary.md").read_text()
        self.assertIn("The persistence check passed.", summary)

    def test_bounded_list_pagination_and_filters(self) -> None:
        first = self.start("first")
        self.store.finish("First done.", "inconclusive")
        second = self.start("second")
        page = self.store.list(limit=1)
        self.assertEqual(len(page["experiments"]), 1)
        self.assertIsNotNone(page["next_offset"])
        active = self.store.list(status="active", tag="phase-2c", limit=10)
        self.assertEqual(
            [item["experiment_id"] for item in active["experiments"]],
            [second["experiment_id"]],
        )
        finished = self.store.list(status="finished", limit=10)
        self.assertEqual(
            [item["experiment_id"] for item in finished["experiments"]],
            [first["experiment_id"]],
        )

    def test_recovery_after_store_reconstruction(self) -> None:
        experiment_id = self.start()["experiment_id"]
        recovered = ExperimentStore(self.root)
        self.assertEqual(recovered.current_id(), experiment_id)
        recovered.note("Recovered after MCP reconstruction.")
        self.assertEqual(recovered.get(experiment_id, 1)["events"][0]["event"], "note")

    def test_recovery_rebuilds_missing_current_pointer(self) -> None:
        experiment_id = self.start()["experiment_id"]
        (self.root / "current.json").unlink()
        recovered = ExperimentStore(self.root)
        self.assertEqual(recovered.current_id(), experiment_id)
        self.assertTrue((self.root / "current.json").is_file())

    def test_corrupt_manifest_is_reported(self) -> None:
        experiment_id = self.start()["experiment_id"]
        manifest_path = self.root / experiment_id / "manifest.json"
        manifest_path.write_text("{malformed", encoding="utf-8")
        with self.assertRaises(ExperimentCorruptError):
            self.store.get(experiment_id)

    def test_missing_experiment_is_reported(self) -> None:
        with self.assertRaises(ExperimentNotFoundError):
            self.store.get(generate_experiment_id("missing"))


class DeliverableSafetyTests(unittest.TestCase):
    def test_no_obvious_credential_literals_or_generated_cache_files(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        suspicious = [
            re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
            re.compile(r"nvapi-[A-Za-z0-9_-]{20,}"),
            re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
        ]
        for path in package_root.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts:
                continue
            self.assertNotEqual(path.name, ".env")
            self.assertNotEqual(path.suffix, ".pyc")
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for pattern in suspicious:
                self.assertIsNone(pattern.search(text), f"Suspicious literal in {path}")


if __name__ == "__main__":
    unittest.main()
