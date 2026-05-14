from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from teams_runtime.core.template import scaffold_workspace


def _load_script_module():
    repo_root = Path(__file__).resolve().parent.parent
    script_path = (
        repo_root
        / "templates"
        / "scaffold"
        / "qa"
        / ".agents"
        / "skills"
        / "discord_message_read"
        / "scripts"
        / "read_discord_message.py"
    )
    spec = importlib.util.spec_from_file_location("qa_discord_message_read_script", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load script module from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TeamsRuntimeQaDiscordMessageReadSkillTests(unittest.TestCase):
    def test_script_reads_message_with_qa_token_env_and_optional_context(self):
        module = _load_script_module()
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request)
            self.assertEqual(timeout, 30)
            self.assertEqual(request.get_header("Authorization"), "Bot qa-token")
            if request.full_url.endswith("/channels/111/messages/222"):
                return _FakeResponse(
                    {
                        "id": "222",
                        "channel_id": "111",
                        "guild_id": "333",
                        "author": {"id": "444", "username": "qa-bot", "bot": True},
                        "content": "배포 준비 완료",
                        "timestamp": "2026-05-14T01:00:00+00:00",
                        "attachments": [],
                        "embeds": [],
                        "components": [],
                        "mention_everyone": False,
                        "mention_roles": [],
                        "mentions": [],
                    }
                )
            self.assertIn("/channels/111/messages?around=222&limit=3", request.full_url)
            return _FakeResponse(
                [
                    {
                        "id": "222",
                        "channel_id": "111",
                        "author": {"id": "444", "username": "qa-bot", "bot": True},
                        "content": "배포 준비 완료",
                        "timestamp": "2026-05-14T01:00:00+00:00",
                        "attachments": [],
                        "embeds": [],
                        "components": [],
                        "mention_everyone": False,
                        "mention_roles": [],
                        "mentions": [],
                    }
                ]
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            scaffold_workspace(workspace_root)
            (workspace_root / ".env").write_text("AGENT_DISCORD_TOKEN_QA=qa-token\n", encoding="utf-8")

            stdout = io.StringIO()
            with patch.object(module, "urlopen", side_effect=fake_urlopen):
                with redirect_stdout(stdout):
                    exit_code = module.main(
                        [
                            "--workspace-root",
                            str(workspace_root),
                            "--channel-id",
                            "111",
                            "--message-id",
                            "222",
                            "--around",
                            "3",
                        ]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["token_env"], "AGENT_DISCORD_TOKEN_QA")
        self.assertEqual(payload["message"]["content"], "배포 준비 완료")
        self.assertEqual(payload["message"]["author"]["id"], "444")
        self.assertEqual(len(payload["context"]), 1)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("qa-token", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
