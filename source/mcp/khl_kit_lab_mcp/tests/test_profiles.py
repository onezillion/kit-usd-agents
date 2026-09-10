import json
import os
import tempfile
import unittest
from pathlib import Path

from khl_kit_lab_mcp.profiles import ProfileStore, ProfileStoreError, validate_capture_id


class ProfileStoreTests(unittest.TestCase):
    def test_private_no_overwrite_capture_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "profiles"
            store = ProfileStore(root)
            allocation = store.create({"duration_seconds": 1.0})
            capture_id = allocation["capture_id"]
            self.assertEqual(oct(root.stat().st_mode & 0o777), "0o700")
            self.assertEqual(oct(Path(allocation["request_path"]).stat().st_mode & 0o777), "0o600")
            output = store.finish(capture_id, {"ok": True, "result": {"marker_found": True}})
            self.assertGreater(output["actual_bytes"], 0)
            evidence = store.get(capture_id)
            self.assertTrue(evidence["complete"])
            self.assertTrue(evidence["result"]["result"]["result"]["marker_found"])
            with self.assertRaises(ProfileStoreError):
                store.finish(capture_id, {"ok": True})
            Path(allocation["request_path"]).chmod(0o644)
            with self.assertRaises(ProfileStoreError):
                store.get(capture_id)

    def test_invalid_ids_traversal_and_symlinks_are_rejected(self):
        for capture_id in ("../escape", "A" * 32, "0" * 31, "${HOME}"):
            with self.subTest(capture_id=capture_id), self.assertRaises(ProfileStoreError):
                validate_capture_id(capture_id)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            real = base / "real"
            real.mkdir(mode=0o700)
            link = base / "profiles"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(ProfileStoreError):
                ProfileStore(link).create({})

    def test_unsafe_permissions_corrupt_and_oversize_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "profiles"
            root.mkdir(mode=0o755)
            with self.assertRaises(ProfileStoreError):
                ProfileStore(root).create({})

            root.chmod(0o700)
            store = ProfileStore(root)
            allocation = store.create({})
            capture_id = allocation["capture_id"]
            result = Path(allocation["directory"]) / "result.json"
            result.write_text("not-json", encoding="utf-8")
            result.chmod(0o600)
            with self.assertRaises(json.JSONDecodeError):
                store.get(capture_id)

            Path(allocation["directory"]).chmod(0o755)
            with self.assertRaises(ProfileStoreError):
                store.get(capture_id)

    def test_root_parent_must_exist_and_is_not_created_recursively(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "missing" / "profiles"
            with self.assertRaises(FileNotFoundError):
                ProfileStore(root).create({})


if __name__ == "__main__":
    unittest.main()
