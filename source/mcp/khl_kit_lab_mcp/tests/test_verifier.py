import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from khl_kit_lab_mcp.experiments import ExperimentStore


spec = importlib.util.spec_from_file_location("kit_lab_verifier", Path(__file__).resolve().parents[1] / "verify.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class VerifierTests(unittest.TestCase):
    def test_default_cli_does_not_select_consequential_mode(self):
        for arguments, selected in (([], "read_only_verification"), (["--full"], "live_verification")):
            with self.subTest(arguments=arguments), patch("sys.argv", ["verify.py", *arguments]), \
                    patch.object(verifier, "version", return_value="2.1.1"), \
                    patch.object(verifier, "read_only_verification", AsyncMock()) as read, \
                    patch.object(verifier, "live_verification", AsyncMock()) as full:
                verifier.main()
                (read if selected == "read_only_verification" else full).assert_awaited_once()
                (full if selected == "read_only_verification" else read).assert_not_called()

    def test_default_live_calls_are_only_read_only_tools(self):
        response = SimpleNamespace(is_error=False, structured_content={
            "ok": True, "result": {"statistics_computed": False, "prim_count": None, "type_counts": None}})
        connected = SimpleNamespace(call_tool=AsyncMock(return_value=response))
        client = AsyncMock()
        client.__aenter__.return_value = connected
        with patch.object(verifier, "Client", return_value=client), \
                patch.object(verifier, "check_tools", AsyncMock()):
            asyncio.run(verifier.read_only_verification("unused"))
        self.assertEqual([c.args[0] for c in connected.call_tool.await_args_list],
                         ["kit_lab_status", "kit_stage_summary", "kit_experiment_current"])

    def test_old_summary_and_misleading_zero_are_rejected(self):
        for result in ({"prim_count": 0}, {"statistics_computed": True, "prim_count": 0},
                       {"statistics_computed": False, "prim_count": 0}):
            with self.subTest(result=result), self.assertRaises(SystemExit):
                verifier.validate_summary({"result": result})

    def test_historical_evidence_references_are_checked_without_retired_tool_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ExperimentStore(Path(temporary) / "records")
            eid = store.start("Historical", "Preserve evidence", [], {})["experiment_id"]
            event = store.record_operation(eid, "kit_prim_create", {}, success=True,
                                           elapsed_ms=1, result={"ok": True}, python_source="legacy source")
            with patch.object(verifier, "experiment_directory", return_value=store.root / eid):
                verifier.verify_record_files(eid, [event])
                with self.assertRaises(SystemExit):
                    verifier.verify_record_files(eid, [{**event, "result_file": None}])
                with self.assertRaises(SystemExit):
                    verifier.verify_record_files(eid, [{**event, "python_source_file": "missing.py"}])


if __name__ == "__main__":
    unittest.main()
