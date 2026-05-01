from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import teams_runtime.discord.client as client_module
from teams_runtime.discord.client import (
    DiscordAttachment,
    DiscordClient,
    DiscordListenError,
    DiscordMessage,
    DiscordSendError,
    MESSAGE_END_MARKER,
    MESSAGE_START_MARKER,
    classify_discord_exception,
    strip_message_boundary_markers,
)


class _FakeIntents:
    def __init__(self):
        self.guilds = False
        self.messages = False
        self.guild_messages = False
        self.dm_messages = False
        self.message_content = None

    @classmethod
    def default(cls):
        return cls()


class _FakeSDKClient:
    def __init__(self, intents=None):
        self.intents = intents
        self.user = None

    def event(self, fn):
        return fn


class _FakeEmbed:
    def __init__(self, *, title=None, description=None, color=None):
        self.title = title
        self.description = description
        self.color = color
        self.fields = []

    def add_field(self, *, name, value, inline=False):
        self.fields.append({"name": name, "value": value, "inline": inline})


class _FakeAllowedMentions:
    @classmethod
    def none(cls):
        return cls()

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeFile:
    def __init__(self, path):
        self.path = path
        self.closed = False

    def close(self):
        self.closed = True


class _ReadySDKClient:
    def __init__(self, intents=None):
        self.intents = intents
        self.user = None
        self._events = {}
        self.closed = False

    def event(self, fn):
        self._events[fn.__name__] = fn
        return fn

    async def start(self, _token):
        self.user = SimpleNamespace(id="999", name="PlannerBot")
        on_ready = self._events.get("on_ready")
        if on_ready is not None:
            await on_ready()

    async def close(self):
        self.closed = True


class _FakeDiscordModule:
    Intents = _FakeIntents
    Client = _FakeSDKClient
    Embed = _FakeEmbed
    AllowedMentions = _FakeAllowedMentions
    File = _FakeFile


class _ReadyDiscordModule:
    Intents = _FakeIntents
    Client = _ReadySDKClient


class DiscordClientIntentTests(unittest.TestCase):
    def test_classify_discord_exception_identifies_client_disconnected_message(self):
        diagnostics = classify_discord_exception(RuntimeError("Discord client disconnected during gateway resume"))

        self.assertEqual(diagnostics["category"], "client_disconnected")
        self.assertIn("disconnected", diagnostics["summary"].lower())

    def test_classify_discord_exception_identifies_dns_failure(self):
        diagnostics = classify_discord_exception(
            RuntimeError("Cannot connect to host discord.com:443 ssl:default [nodename nor servname provided, or not known]")
        )

        self.assertEqual(diagnostics["category"], "discord_dns_failed")
        self.assertIn("dns", diagnostics["recovery_action"].lower())

    def test_classify_discord_exception_identifies_missing_discord_sdk(self):
        diagnostics = classify_discord_exception(
            RuntimeError("discord.py is not installed. Install teams_runtime/requirements.txt.")
        )

        self.assertEqual(diagnostics["category"], "discord_sdk_missing")
        self.assertIn("discord.py", diagnostics["summary"])

    def test_strip_message_boundary_markers_only_removes_outer_wrapper(self):
        content = (
            f"{MESSAGE_START_MARKER}\n"
            f"본문 안의 {MESSAGE_START_MARKER} 와 {MESSAGE_END_MARKER} 는 유지되어야 함\n"
            f"{MESSAGE_END_MARKER}"
        )

        self.assertEqual(
            strip_message_boundary_markers(content),
            f"본문 안의 {MESSAGE_START_MARKER} 와 {MESSAGE_END_MARKER} 는 유지되어야 함",
        )

    def test_strip_message_boundary_markers_preserves_leading_mention_prefix(self):
        content = (
            "<@111111111111111117>\n"
            f"{MESSAGE_START_MARKER}\n"
            "request_id: 20260325-0664130c\n"
            "intent: route\n"
            "scope: orchestrator routing bug\n"
            f"{MESSAGE_END_MARKER}"
        )

        self.assertEqual(
            strip_message_boundary_markers(content),
            "<@111111111111111117>\n"
            "request_id: 20260325-0664130c\n"
            "intent: route\n"
            "scope: orchestrator routing bug",
        )

    def test_message_content_intent_is_disabled_by_default(self):
        with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
            with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                client = DiscordClient(token_env=None)
                sdk_client, _ready = client._build_sdk_client()
                self.assertFalse(sdk_client.intents.message_content)

    def test_client_reloads_token_from_dotenv_with_override(self):
        def fake_load_dotenv(*args, **kwargs):
            self.assertTrue(kwargs.get("override"))
            os.environ["AGENT_DISCORD_TOKEN_QA"] = "fresh-token"

        with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
            with patch.dict(os.environ, {"AGENT_DISCORD_TOKEN_QA": "stale-token"}, clear=False):
                with patch("teams_runtime.discord.client.load_dotenv", side_effect=fake_load_dotenv):
                    client = DiscordClient(token_env="AGENT_DISCORD_TOKEN_QA")
                    self.assertEqual(client._token, "fresh-token")

    def test_client_loads_token_from_parent_dotenv_when_started_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "teams_generated"
            workspace.mkdir()
            (root / ".env").write_text("AGENT_DISCORD_TOKEN_CS_ADMIN=parent-token\n", encoding="utf-8")
            current = Path.cwd()
            try:
                os.chdir(workspace)
                with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
                    with patch.dict(os.environ, {}, clear=True):
                        client = DiscordClient(token_env="AGENT_DISCORD_TOKEN_CS_ADMIN")
                self.assertEqual(client._token, "parent-token")
            finally:
                os.chdir(current)

    def test_message_content_intent_can_be_enabled_explicitly(self):
        with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
            with patch.dict(
                os.environ,
                {
                    "DISCORD_TOKEN": "token",
                    "TEAMS_RUNTIME_ENABLE_MESSAGE_CONTENT_INTENT": "true",
                },
                clear=False,
            ):
                client = DiscordClient(token_env=None)
                sdk_client, _ready = client._build_sdk_client()
                self.assertTrue(sdk_client.intents.message_content)

    def test_outbound_discord_messages_are_logged_under_transcript_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "planner.jsonl"
            with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
                with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                    client = DiscordClient(
                        token_env=None,
                        transcript_log_file=log_file,
                        client_name="planner",
                    )
                    client._execute_send = AsyncMock(
                        return_value=DiscordMessage(
                            message_id="1",
                            channel_id="200",
                            guild_id="300",
                            author_id="400",
                            author_name="planner",
                            content="hello",
                            is_dm=False,
                            mentions_bot=False,
                            created_at=datetime.now(timezone.utc),
                        )
                    )

                    asyncio.run(client.send_channel_message("200", "hello"))

                    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line]
                    self.assertEqual(len(entries), 1)
                    self.assertEqual(entries[0]["client"], "planner")
                    self.assertEqual(entries[0]["direction"], "outbound")
                    self.assertEqual(entries[0]["transport"], "channel")
                    self.assertEqual(entries[0]["status"], "sent")
                    self.assertTrue(entries[0]["timestamp"].endswith("+09:00"))
                    self.assertTrue(entries[0]["message"]["created_at"].endswith("+09:00"))

    def test_send_channel_rich_message_passes_embed_files_allowed_mentions_and_logs_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attachment = Path(tmpdir) / "report.md"
            attachment.write_text("# Report\n", encoding="utf-8")
            log_file = Path(tmpdir) / "planner.jsonl"
            with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
                with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                    client = DiscordClient(
                        token_env=None,
                        transcript_log_file=log_file,
                        client_name="planner",
                    )
                    observed: dict[str, object] = {}

                    async def fake_execute(operation):
                        sent_channel = SimpleNamespace(
                            id=200,
                            guild=SimpleNamespace(id=300),
                            send=AsyncMock(
                                return_value=SimpleNamespace(
                                    id="msg-rich",
                                    channel=SimpleNamespace(id=200),
                                    guild=SimpleNamespace(id=300),
                                    author=SimpleNamespace(id="999", name="bot", display_name="bot"),
                                    content="",
                                    mentions=[],
                                    attachments=[],
                                    created_at=datetime.now(timezone.utc),
                                )
                            ),
                        )
                        sdk_client = SimpleNamespace(fetch_channel=AsyncMock(return_value=sent_channel))
                        result = await operation(sdk_client)
                        observed["send_kwargs"] = sent_channel.send.await_args.kwargs
                        return result

                    client._execute_send = fake_execute

                    asyncio.run(
                        client.send_channel_rich_message(
                            "200",
                            embed={
                                "title": "Sprint done",
                                "description": "summary",
                                "fields": [{"name": "Todo", "value": "completed:1"}],
                                "color": 123,
                            },
                            files=[attachment],
                            allowed_mentions="none",
                        )
                    )

                    kwargs = observed["send_kwargs"]
                    self.assertEqual(kwargs["embed"].title, "Sprint done")
                    self.assertEqual(kwargs["embed"].fields[0]["name"], "Todo")
                    self.assertEqual(Path(kwargs["files"][0].path), attachment)
                    self.assertIsInstance(kwargs["allowed_mentions"], _FakeAllowedMentions)
                    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line]
                    self.assertTrue(entries[0]["metadata"]["rich"])
                    self.assertEqual(entries[0]["metadata"]["files"], ["report.md"])
                    self.assertEqual(entries[0]["metadata"]["allowed_mentions"], "none")

    def test_send_channel_message_retries_transient_fetch_channel_timeout_then_succeeds(self):
        with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
            with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                client = DiscordClient(token_env=None)
                sent_channel = SimpleNamespace(
                    id=200,
                    guild=SimpleNamespace(id=300),
                    send=AsyncMock(
                        return_value=SimpleNamespace(
                            id="msg-200",
                            channel=SimpleNamespace(id=200),
                            guild=SimpleNamespace(id=300),
                            author=SimpleNamespace(id="999", name="bot", display_name="bot"),
                            content="hello",
                            mentions=[],
                            created_at=datetime.now(timezone.utc),
                        )
                    ),
                )
                temporary_client = SimpleNamespace(
                    login=AsyncMock(),
                    close=AsyncMock(),
                    fetch_channel=AsyncMock(side_effect=[asyncio.TimeoutError(), sent_channel]),
                )

                with patch.object(client, "_build_sdk_client", return_value=(temporary_client, asyncio.Event())):
                    with patch("teams_runtime.discord.client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
                        result = asyncio.run(client.send_channel_message("200", "hello"))

                self.assertEqual(result.channel_id, "200")
                self.assertEqual(temporary_client.fetch_channel.await_count, 2)
                self.assertEqual(sent_channel.send.await_count, 1)
                self.assertGreaterEqual(sleep_mock.await_count, 1)

    def test_send_channel_message_records_retry_metadata_on_final_timeout_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "planner.jsonl"
            with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
                with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                    client = DiscordClient(
                        token_env=None,
                        transcript_log_file=log_file,
                        client_name="planner",
                    )
                    temporary_client = SimpleNamespace(
                        login=AsyncMock(),
                        close=AsyncMock(),
                        fetch_channel=AsyncMock(side_effect=asyncio.TimeoutError()),
                    )

                    with patch.object(client, "_build_sdk_client", return_value=(temporary_client, asyncio.Event())):
                        with patch("teams_runtime.discord.client.asyncio.sleep", new=AsyncMock()):
                            with self.assertRaises(DiscordSendError) as caught:
                                asyncio.run(client.send_channel_message("200", "hello"))

                    self.assertEqual(caught.exception.attempts, 3)
                    self.assertTrue(caught.exception.retryable)
                    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line]
                    self.assertEqual(entries[-1]["status"], "failed")
                    self.assertEqual(entries[-1]["metadata"]["attempts"], 3)
                    self.assertEqual(entries[-1]["metadata"]["phase"], "fetch_channel(200)")
                    self.assertEqual(entries[-1]["metadata"]["last_error_type"], "TimeoutError")

    def test_send_channel_message_retries_transient_channel_send_timeout_then_succeeds(self):
        with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
            with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                client = DiscordClient(token_env=None)
                sent_channel = SimpleNamespace(
                    id=200,
                    guild=SimpleNamespace(id=300),
                    send=AsyncMock(
                        side_effect=[
                            asyncio.TimeoutError(),
                            SimpleNamespace(
                                id="msg-200",
                                channel=SimpleNamespace(id=200),
                                guild=SimpleNamespace(id=300),
                                author=SimpleNamespace(id="999", name="bot", display_name="bot"),
                                content="hello",
                                mentions=[],
                                created_at=datetime.now(timezone.utc),
                            ),
                        ]
                    ),
                )
                temporary_client = SimpleNamespace(
                    login=AsyncMock(),
                    close=AsyncMock(),
                    fetch_channel=AsyncMock(return_value=sent_channel),
                )

                with patch.object(client, "_build_sdk_client", return_value=(temporary_client, asyncio.Event())):
                    with patch("teams_runtime.discord.client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
                        result = asyncio.run(client.send_channel_message("200", "hello"))

                self.assertEqual(result.channel_id, "200")
                self.assertEqual(temporary_client.fetch_channel.await_count, 2)
                self.assertEqual(sent_channel.send.await_count, 2)
                self.assertGreaterEqual(sleep_mock.await_count, 1)

    def test_send_channel_message_records_retry_metadata_on_final_channel_send_timeout_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "planner.jsonl"
            with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
                with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                    client = DiscordClient(
                        token_env=None,
                        transcript_log_file=log_file,
                        client_name="planner",
                    )
                    sent_channel = SimpleNamespace(
                        id=200,
                        guild=SimpleNamespace(id=300),
                        send=AsyncMock(side_effect=asyncio.TimeoutError()),
                    )
                    temporary_client = SimpleNamespace(
                        login=AsyncMock(),
                        close=AsyncMock(),
                        fetch_channel=AsyncMock(return_value=sent_channel),
                    )

                    with patch.object(client, "_build_sdk_client", return_value=(temporary_client, asyncio.Event())):
                        with patch("teams_runtime.discord.client.asyncio.sleep", new=AsyncMock()):
                            with self.assertRaises(DiscordSendError) as caught:
                                asyncio.run(client.send_channel_message("200", "hello"))

                    self.assertEqual(caught.exception.attempts, 3)
                    self.assertEqual(caught.exception.phase, "channel.send(200)")
                    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line]
                    self.assertEqual(entries[-1]["status"], "failed")
                    self.assertEqual(entries[-1]["metadata"]["attempts"], 3)
                    self.assertEqual(entries[-1]["metadata"]["phase"], "channel.send(200)")
                    self.assertEqual(entries[-1]["metadata"]["last_error_type"], "TimeoutError")

    def test_inbound_discord_messages_are_logged_under_transcript_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "planner.jsonl"
            with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
                with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                    client = DiscordClient(
                        token_env=None,
                        transcript_log_file=log_file,
                        client_name="planner",
                    )
                    client._client = SimpleNamespace(user=SimpleNamespace(id="999"))

                    async def handler(_message):
                        return None

                    client._message_handler = handler
                    fake_message = SimpleNamespace(
                        author=SimpleNamespace(id="101", bot=False, display_name="user", name="user"),
                        channel=SimpleNamespace(id="202"),
                        guild=None,
                        content="hello inbound",
                        mentions=[],
                        created_at=datetime.now(timezone.utc),
                        id="msg-1",
                    )

                    asyncio.run(client._dispatch_message(fake_message))

                    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line]
                    self.assertEqual(len(entries), 1)
                    self.assertEqual(entries[0]["client"], "planner")
                    self.assertEqual(entries[0]["direction"], "inbound")
                    self.assertEqual(entries[0]["transport"], "dm")
                    self.assertEqual(entries[0]["status"], "received")
                    self.assertTrue(entries[0]["timestamp"].endswith("+09:00"))
                    self.assertTrue(entries[0]["message"]["created_at"].endswith("+09:00"))

    def test_inbound_discord_attachments_are_saved_and_logged_under_transcript_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "planner.jsonl"
            attachment_root = Path(tmpdir) / "attachments"
            observed: dict[str, object] = {}

            class _FakeResponse:
                def read(self):
                    return b"hello attachment"

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
                with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                    client = DiscordClient(
                        token_env=None,
                        transcript_log_file=log_file,
                        attachment_dir=attachment_root,
                        client_name="planner",
                    )
                    client._client = SimpleNamespace(user=SimpleNamespace(id="999"))

                    async def handler(message):
                        observed["attachments"] = message.attachments

                    client._message_handler = handler
                    fake_message = SimpleNamespace(
                        author=SimpleNamespace(id="101", bot=False, display_name="user", name="user"),
                        channel=SimpleNamespace(id="202"),
                        guild=None,
                        content="hello inbound",
                        mentions=[],
                        attachments=[
                            SimpleNamespace(
                                id="att-1",
                                filename="brief?.txt",
                                content_type="text/plain",
                                size=16,
                                url="https://cdn.discord.test/brief.txt",
                                proxy_url="",
                            )
                        ],
                        created_at=datetime.now(timezone.utc),
                        id="msg-attach-1",
                    )

                    with patch("teams_runtime.discord.client.urlopen", return_value=_FakeResponse()):
                        asyncio.run(client._dispatch_message(fake_message))

                    saved_path = attachment_root / "att-1_brief%3F.txt"
                    self.assertTrue(saved_path.exists())
                    self.assertEqual(len(observed["attachments"]), 1)
                    self.assertEqual(observed["attachments"][0].saved_path, str(saved_path.resolve()))

                    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line]
                    self.assertEqual(entries[0]["message"]["attachments"][0]["saved_path"], str(saved_path.resolve()))
                    self.assertEqual(entries[0]["message"]["attachments"][0]["filename"], "brief?.txt")

    def test_inbound_discord_message_refetches_missing_attachments_before_saving(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "planner.jsonl"
            attachment_root = Path(tmpdir) / "attachments"
            observed: dict[str, object] = {}
            fetch_calls: list[str] = []

            class _FakeResponse:
                def read(self):
                    return b"hello attachment"

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
                with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                    client = DiscordClient(
                        token_env=None,
                        transcript_log_file=log_file,
                        attachment_dir=attachment_root,
                        client_name="planner",
                    )
                    client._client = SimpleNamespace(user=SimpleNamespace(id="999"))

                    async def handler(message):
                        observed["attachments"] = message.attachments

                    async def fetch_message(message_id):
                        fetch_calls.append(str(message_id))
                        return SimpleNamespace(
                            author=SimpleNamespace(id="101", bot=False, display_name="user", name="user"),
                            channel=channel,
                            guild=None,
                            content="파일 첨부함",
                            mentions=[],
                            attachments=[
                                SimpleNamespace(
                                    id="att-2",
                                    filename="source.md",
                                    content_type="text/markdown",
                                    size=32,
                                    url="https://cdn.discord.test/source.md",
                                    proxy_url="",
                                )
                            ],
                            created_at=datetime.now(timezone.utc),
                            id=message_id,
                        )

                    channel = SimpleNamespace(id="202", fetch_message=fetch_message)
                    client._message_handler = handler
                    fake_message = SimpleNamespace(
                        author=SimpleNamespace(id="101", bot=False, display_name="user", name="user"),
                        channel=channel,
                        guild=None,
                        content="파일 첨부함",
                        mentions=[],
                        attachments=[],
                        created_at=datetime.now(timezone.utc),
                        id="msg-attach-2",
                    )

                    with patch("teams_runtime.discord.client.urlopen", return_value=_FakeResponse()):
                        asyncio.run(client._dispatch_message(fake_message))

                    saved_path = attachment_root / "att-2_source.md"
                    self.assertEqual(fetch_calls, ["msg-attach-2"])
                    self.assertTrue(saved_path.exists())
                    self.assertEqual(len(observed["attachments"]), 1)
                    self.assertEqual(observed["attachments"][0].saved_path, str(saved_path.resolve()))

                    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line]
                    self.assertEqual(entries[0]["message"]["attachments"][0]["saved_path"], str(saved_path.resolve()))
                    self.assertEqual(entries[0]["message"]["attachments"][0]["filename"], "source.md")

    def test_inbound_discord_attachments_use_dynamic_attachment_dir_resolver(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "planner.jsonl"
            base_attachment_root = Path(tmpdir) / "attachments"
            sprint_attachment_root = Path(tmpdir) / "shared_workspace" / "sprints" / "sprint-a" / "attachments"
            observed: dict[str, object] = {}

            class _FakeResponse:
                def read(self):
                    return b"hello attachment"

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
                with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                    client = DiscordClient(
                        token_env=None,
                        transcript_log_file=log_file,
                        attachment_dir=base_attachment_root,
                        attachment_dir_resolver=lambda _message: sprint_attachment_root,
                        client_name="planner",
                    )
                    client._client = SimpleNamespace(user=SimpleNamespace(id="999"))

                    async def handler(message):
                        observed["attachments"] = message.attachments

                    client._message_handler = handler
                    fake_message = SimpleNamespace(
                        author=SimpleNamespace(id="101", bot=False, display_name="user", name="user"),
                        channel=SimpleNamespace(id="202"),
                        guild=None,
                        content="hello inbound",
                        mentions=[],
                        attachments=[
                            SimpleNamespace(
                                id="att-3",
                                filename="brief.md",
                                content_type="text/markdown",
                                size=16,
                                url="https://cdn.discord.test/brief.md",
                                proxy_url="",
                            )
                        ],
                        created_at=datetime.now(timezone.utc),
                        id="msg-attach-3",
                    )

                    with patch("teams_runtime.discord.client.urlopen", return_value=_FakeResponse()):
                        asyncio.run(client._dispatch_message(fake_message))

                    saved_path = sprint_attachment_root / "att-3_brief.md"
                    self.assertFalse((base_attachment_root / "att-3_brief.md").exists())
                    self.assertTrue(saved_path.exists())
                    self.assertEqual(observed["attachments"][0].saved_path, str(saved_path.resolve()))

    def test_callback_failure_is_logged_without_raising_from_dispatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "planner.jsonl"
            with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
                with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                    client = DiscordClient(
                        token_env=None,
                        transcript_log_file=log_file,
                        client_name="planner",
                    )
                    client._client = SimpleNamespace(user=SimpleNamespace(id="999"))

                    async def handler(_message):
                        raise RuntimeError("boom")

                    client._message_handler = handler
                    fake_message = SimpleNamespace(
                        author=SimpleNamespace(id="101", bot=False, display_name="user", name="user"),
                        channel=SimpleNamespace(id="202"),
                        guild=None,
                        content="hello inbound",
                        mentions=[],
                        created_at=datetime.now(timezone.utc),
                        id="msg-2",
                    )

                    asyncio.run(client._dispatch_message(fake_message))

                    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line]
                    self.assertEqual(len(entries), 2)
                    self.assertEqual(entries[0]["status"], "received")
                    self.assertEqual(entries[1]["status"], "callback_failed")

    def test_chunked_inbound_messages_merge_attachments_across_chunks(self):
        with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
            with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                client = DiscordClient(token_env=None)
                client._client = SimpleNamespace(user=SimpleNamespace(id="999"))
                observed: list[DiscordMessage] = []

                async def handler(message):
                    observed.append(message)

                client._message_handler = handler
                first_chunk = SimpleNamespace(
                    author=SimpleNamespace(id="101", bot=False, display_name="user", name="user"),
                    channel=SimpleNamespace(id="202"),
                    guild=None,
                    content="[1/2]\n첫 번째 줄",
                    mentions=[],
                    attachments=[
                        SimpleNamespace(
                            id="att-1",
                            filename="one.txt",
                            content_type="text/plain",
                            size=1,
                            url="https://cdn.discord.test/one.txt",
                            proxy_url="",
                        )
                    ],
                    created_at=datetime.now(timezone.utc),
                    id="chunk-1",
                )
                second_chunk = SimpleNamespace(
                    author=SimpleNamespace(id="101", bot=False, display_name="user", name="user"),
                    channel=SimpleNamespace(id="202"),
                    guild=None,
                    content="[2/2]\n두 번째 줄",
                    mentions=[],
                    attachments=[
                        SimpleNamespace(
                            id="att-2",
                            filename="two.txt",
                            content_type="text/plain",
                            size=1,
                            url="https://cdn.discord.test/two.txt",
                            proxy_url="",
                        )
                    ],
                    created_at=datetime.now(timezone.utc),
                    id="chunk-2",
                )

                asyncio.run(client._dispatch_message(first_chunk))
                asyncio.run(client._dispatch_message(second_chunk))

                self.assertEqual(len(observed), 1)
                self.assertEqual(observed[0].content, "첫 번째 줄\n두 번째 줄")
                self.assertEqual(
                    {attachment.attachment_id for attachment in observed[0].attachments},
                    {"att-1", "att-2"},
                )

    def test_relay_channel_does_not_forward_human_messages_without_mention(self):
        with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
            with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                client = DiscordClient(
                    token_env=None,
                    always_listen_channel_ids={"relay-channel"},
                )
                client._client = SimpleNamespace(user=SimpleNamespace(id="999"))
                guild_message = SimpleNamespace(
                    author=SimpleNamespace(id="101", bot=False, display_name="user", name="user"),
                    channel=SimpleNamespace(id="relay-channel"),
                    guild=SimpleNamespace(id="guild-1"),
                    content="hello",
                    mentions=[],
                    created_at=datetime.now(timezone.utc),
                    id="msg-1",
                )

                self.assertFalse(client._should_forward_message(guild_message))

    def test_relay_channel_still_forwards_trusted_bot_messages_without_mention(self):
        with patch("teams_runtime.discord.client.discord", _FakeDiscordModule):
            with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=False):
                client = DiscordClient(
                    token_env=None,
                    allowed_bot_author_ids={"trusted-bot"},
                    always_listen_channel_ids={"relay-channel"},
                )
                client._client = SimpleNamespace(user=SimpleNamespace(id="999"))
                relay_message = SimpleNamespace(
                    author=SimpleNamespace(id="trusted-bot", bot=True, display_name="relay", name="relay"),
                    channel=SimpleNamespace(id="relay-channel"),
                    guild=SimpleNamespace(id="guild-1"),
                    content="delegate",
                    mentions=[],
                    created_at=datetime.now(timezone.utc),
                    id="msg-2",
                )

                self.assertTrue(client._should_forward_message(relay_message))

    def test_listen_fails_when_logged_in_bot_id_does_not_match_expected_bot_id(self):
        with patch("teams_runtime.discord.client.discord", _ReadyDiscordModule):
            with patch.dict(os.environ, {"AGENT_DISCORD_TOKEN_QA": "token"}, clear=False):
                client = DiscordClient(
                    token_env="AGENT_DISCORD_TOKEN_QA",
                    expected_bot_id="123",
                    client_name="qa",
                )

                async def handler(_message):
                    return None

                with self.assertRaisesRegex(
                    DiscordListenError,
                    "expected bot_id 123, got 999 \\(PlannerBot\\) from AGENT_DISCORD_TOKEN_QA",
                ):
                    asyncio.run(client.listen(handler))

    def test_resilient_sdk_client_falls_back_to_fresh_session_without_ws_state(self):
        resilient_client_cls = client_module._ResilientDiscordSDKClient
        if resilient_client_cls is None:
            self.skipTest("discord.py is not installed")

        sdk_client = object.__new__(resilient_client_cls)
        sdk_client.shard_id = None
        sdk_client.ws = None
        dispatched: list[str] = []
        sdk_client.dispatch = lambda event: dispatched.append(str(event))
        sdk_client.is_closed = lambda: False
        sdk_client.close = AsyncMock()

        call_params: list[dict[str, object]] = []

        class _FakeBackoff:
            def delay(self) -> float:
                return 0.0

        class _FakeWebSocket:
            gateway = "wss://gateway.discord.test"
            session_id = "session-1"
            sequence = 42

            async def poll_event(self) -> None:
                raise asyncio.CancelledError()

        async def fake_from_client(_client, **kwargs):
            call_params.append(dict(kwargs))
            if len(call_params) == 1:
                raise client_module.aiohttp.ClientError("dns failed")
            return _FakeWebSocket()

        with patch.object(client_module, "ExponentialBackoff", _FakeBackoff):
            with patch.object(client_module, "DiscordWebSocket", SimpleNamespace(from_client=fake_from_client)):
                with patch("teams_runtime.discord.client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
                    with patch.object(client_module.DISCORD_SDK_LOGGER, "warning") as warning_mock:
                        with patch.object(client_module.DISCORD_SDK_LOGGER, "exception") as exception_mock:
                            with self.assertRaises(asyncio.CancelledError):
                                asyncio.run(sdk_client.connect())

        self.assertEqual(dispatched, ["disconnect"])
        self.assertEqual(sleep_mock.await_count, 1)
        self.assertEqual(len(call_params), 2)
        self.assertEqual(call_params[0], {"initial": True, "shard_id": None})
        self.assertEqual(call_params[1], {"initial": False, "shard_id": None})
        exception_mock.assert_not_called()
        self.assertGreaterEqual(warning_mock.call_count, 2)
        warning_messages = [str(call.args[0]) for call in warning_mock.call_args_list]
        self.assertIn("Discord websocket reconnect scheduled in %.2fs after %s: %s", warning_messages)
