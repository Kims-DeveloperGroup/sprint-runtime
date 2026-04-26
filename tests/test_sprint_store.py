from __future__ import annotations

import tempfile
import unittest

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.workflows.state.sprint_store import (
    append_sprint_event,
    iter_sprint_event_entries,
    load_sprint_state,
    save_sprint_state,
)


class TeamsRuntimeSprintStoreTests(unittest.TestCase):
    def test_save_and_load_sprint_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            sprint_state = {
                "sprint_id": "260419-Sprint-22:10",
                "status": "running",
                "milestone_title": "state store extraction",
                "todos": [],
            }

            save_sprint_state(paths, sprint_state, update_timestamp=True, write_current_sprint=True)

            loaded = load_sprint_state(paths, "260419-Sprint-22:10")
            self.assertEqual(loaded["sprint_id"], "260419-Sprint-22:10")
            self.assertEqual(loaded["status"], "running")
            self.assertTrue(loaded["updated_at"])
            self.assertIn("state store extraction", paths.current_sprint_file.read_text(encoding="utf-8"))

    def test_append_and_iter_sprint_event_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)

            append_sprint_event(
                paths,
                "260419-Sprint-22:10",
                event_type="started",
                summary="Sprint started.",
                payload={"trigger": "manual"},
            )

            events = iter_sprint_event_entries(paths, "260419-Sprint-22:10")
            self.assertEqual(events[0]["type"], "started")
            self.assertEqual(events[0]["payload"]["trigger"], "manual")
