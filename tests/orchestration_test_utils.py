from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import teams_runtime.core.orchestration as orchestration_module
from teams_runtime.core.notifications import (
    _render_discord_message_chunks,
    _split_discord_chunks,
    summarize_boxed_report_excerpt,
)
from teams_runtime.core.parsing import envelope_to_text
from teams_runtime.core.reports import build_progress_report
from teams_runtime.core.sprints import (
    build_backlog_item,
    build_sprint_artifact_folder_name,
    build_todo_item,
)
from teams_runtime.core.orchestration import (
    INTERNAL_RELAY_SUMMARY_MARKER,
    TeamService,
    _parse_report_body_json,
)
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.shared.persistence import read_json, write_json
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.workflows.state.backlog_store import merge_backlog_payload
from teams_runtime.discord.client import (
    DiscordAttachment,
    DiscordListenError,
    DiscordSendError,
    DiscordMessage,
    MESSAGE_END_MARKER,
    MESSAGE_START_MARKER,
)
from teams_runtime.models import MessageEnvelope, RoleRuntimeConfig


class FakeDiscordClient:
    def __init__(self, *args, **kwargs):
        self.sent_channels: list[tuple[str, str]] = []
        self.sent_dms: list[tuple[str, str]] = []

    async def listen(self, on_message, on_ready=None):
        if on_ready is not None:
            result = on_ready()
            if asyncio.iscoroutine(result):
                await result
        return None

    async def send_channel_message(self, channel_id, content):
        self.sent_channels.append((str(channel_id), str(content)))
        return DiscordMessage(
            message_id="1",
            channel_id=str(channel_id),
            guild_id="1",
            author_id="999",
            author_name="bot",
            content=str(content),
            is_dm=False,
            mentions_bot=False,
            created_at=datetime.now(timezone.utc),
        )

    async def send_dm(self, user_id, content):
        self.sent_dms.append((str(user_id), str(content)))
        return DiscordMessage(
            message_id="2",
            channel_id="dm",
            guild_id=None,
            author_id="999",
            author_name="bot",
            content=str(content),
            is_dm=True,
            mentions_bot=False,
            created_at=datetime.now(timezone.utc),
        )

    async def close(self):
        return None


class FailingStartupDiscordClient(FakeDiscordClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed_startup = False

    async def send_channel_message(self, channel_id, content):
        normalized_channel_id = str(channel_id)
        if normalized_channel_id == "111111111111111111" and not self.failed_startup:
            self.failed_startup = True
            raise DiscordSendError(
                "Discord send operation failed during fetch_channel(111111111111111111) after 3 attempt(s): TimeoutError: timeout",
                attempts=3,
                last_error=asyncio.TimeoutError("timeout"),
                retryable=True,
                phase="fetch_channel(111111111111111111)",
            )
        return await super().send_channel_message(channel_id, content)


class FailingRelayDiscordClient(FakeDiscordClient):
    async def send_channel_message(self, channel_id, content):
        raise DiscordSendError(
            "Discord send operation failed during channel.send(111111111111111111) after 3 attempt(s): TimeoutError: timeout",
            attempts=3,
            last_error=asyncio.TimeoutError("timeout"),
            retryable=True,
            phase="channel.send(111111111111111111)",
        )


class FailingSourcerSendDiscordClient(FakeDiscordClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client_name = str(kwargs.get("client_name") or "")

    async def send_channel_message(self, channel_id, content):
        if self.client_name == "sourcer":
            raise DiscordSendError(
                "Discord send operation failed during channel.send(1486503058765779066) after 3 attempt(s): TimeoutError: timeout",
                attempts=3,
                last_error=asyncio.TimeoutError("timeout"),
                retryable=True,
                phase="channel.send(1486503058765779066)",
            )
        return await super().send_channel_message(channel_id, content)


class FailingSourcerDnsDiscordClient(FakeDiscordClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client_name = str(kwargs.get("client_name") or "")

    async def send_channel_message(self, channel_id, content):
        if self.client_name == "sourcer":
            dns_error = RuntimeError(
                "Cannot connect to host discord.com:443 ssl:default [nodename nor servname provided, or not known]"
            )
            raise DiscordSendError(
                "Discord send operation failed during channel.send(1486503058765779066) after 3 attempt(s): ClientConnectorDNSError: Cannot connect to host discord.com:443 ssl:default [nodename nor servname provided, or not known]",
                attempts=3,
                last_error=dns_error,
                retryable=True,
                phase="channel.send(1486503058765779066)",
            )
        return await super().send_channel_message(channel_id, content)


class OrchestrationTestCase(unittest.TestCase):
    @staticmethod
    def _workflow_phase_for_step(step):
        return {
            "research_initial": "planning",
            "planner_draft": "planning",
            "planner_advisory": "planning",
            "planner_finalize": "planning",
            "architect_guidance": "implementation",
            "developer_build": "implementation",
            "architect_review": "implementation",
            "developer_revision": "implementation",
            "qa_validation": "validation",
            "closeout": "closeout",
        }[step]

    @staticmethod
    def _workflow_role_for_step(step):
        return {
            "research_initial": "research",
            "planner_draft": "planner",
            "planner_advisory": "designer",
            "planner_finalize": "planner",
            "architect_guidance": "architect",
            "developer_build": "developer",
            "architect_review": "architect",
            "developer_revision": "developer",
            "qa_validation": "qa",
            "closeout": "version_controller",
        }[step]

    def _make_workflow_request_record(
        self,
        *,
        step,
        phase=None,
        current_role=None,
        planning_pass_count=0,
        planning_pass_limit=2,
        review_cycle_count=0,
        review_cycle_limit=3,
        reopen_source_role="",
        reopen_category="",
    ):
        resolved_phase = phase or self._workflow_phase_for_step(step)
        resolved_role = current_role or self._workflow_role_for_step(step)
        return {
            "request_id": f"20260413-{step}-{resolved_role}",
            "status": "delegated",
            "intent": "implement",
            "urgency": "normal",
            "scope": f"workflow transition {step}",
            "body": f"workflow transition {step}",
            "artifacts": [],
            "params": {
                "_teams_kind": "sprint_internal",
                "workflow": {
                    "contract_version": 1,
                    "phase": resolved_phase,
                    "step": step,
                    "phase_owner": resolved_role,
                    "phase_status": "active",
                    "planning_pass_count": planning_pass_count,
                    "planning_pass_limit": planning_pass_limit,
                    "planning_final_owner": "planner",
                    "reopen_source_role": reopen_source_role,
                    "reopen_category": reopen_category,
                    "review_cycle_count": review_cycle_count,
                    "review_cycle_limit": review_cycle_limit,
                },
            },
            "current_role": resolved_role,
            "next_role": resolved_role,
            "owner_role": "orchestrator",
            "sprint_id": "2026-Sprint-Workflow-Matrix",
            "backlog_id": "backlog-1",
            "todo_id": "todo-1",
            "created_at": "2026-04-13T00:00:00+00:00",
            "updated_at": "2026-04-13T00:00:00+00:00",
            "fingerprint": f"workflow-{step}-{resolved_role}",
            "reply_route": {},
            "events": [],
            "result": {},
        }

    def _make_workflow_result(
        self,
        *,
        role,
        summary,
        outcome,
        target_phase="",
        target_step="",
        requested_role="",
        reopen_category="",
        finalize_phase=False,
        status="completed",
        artifacts=None,
        extra_proposals=None,
    ):
        proposals = {
            "workflow_transition": {
                "outcome": outcome,
                "target_phase": target_phase,
                "target_step": target_step,
                "requested_role": requested_role,
                "reopen_category": reopen_category,
                "reason": summary,
                "unresolved_items": [],
                "finalize_phase": finalize_phase,
            }
        }
        if extra_proposals:
            proposals.update(extra_proposals)
        return {
            "request_id": f"result-{role}",
            "role": role,
            "status": status,
            "summary": summary,
            "insights": [],
            "proposals": proposals,
            "artifacts": list(artifacts or []),
            "next_role": "",
            "approval_needed": False,
            "error": "",
        }


__all__ = [name for name in globals() if not name.startswith("__")]
