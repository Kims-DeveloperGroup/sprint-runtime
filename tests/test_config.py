from __future__ import annotations

import asyncio
import io
import os
import signal
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from teams_runtime.cli import (
    DEFAULT_WORKSPACE_DIRNAME,
    InternalAgentService,
    cmd_config_role_set,
    cmd_start,
    cmd_sprint_restart,
    cmd_sprint_start,
    cmd_sprint_stop,
    is_workspace_root,
    main,
    resolve_workspace_root,
)
from teams_runtime.core.agent_capabilities import load_agent_utilization_policy
from teams_runtime.core.config import (
    load_discord_agents_config,
    load_team_runtime_config,
    update_team_runtime_role_defaults,
    validate_runtime_discord_agents_config,
)
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.persistence import new_backlog_id, new_request_id, new_todo_id, read_json, write_json
from teams_runtime.core.sprints import (
    build_active_sprint_id,
    compute_next_slot_at,
    load_sprint_history_index,
    render_current_sprint_markdown,
    render_sprint_history_index,
    render_sprint_history_markdown,
)
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.discord.client import DiscordListenError
from teams_runtime.discord.lifecycle import (
    build_background_command,
    build_background_env,
    is_process_running,
    role_service_status,
    run_foreground_role_service,
    start_background_role_service,
    stop_background_role_service,
)


class TeamsRuntimeConfigTests(unittest.TestCase):
    @staticmethod
    def _write_runtime_ready_discord_config(workspace_root: str | Path) -> None:
        config_path = Path(workspace_root) / "discord_agents_config.yaml"
        content = config_path.read_text(encoding="utf-8")
        replacements = {
            "111111111111111111": "123456789012345678",
            "111111111111111112": "123456789012345679",
            "111111111111111113": "123456789012345680",
            "111111111111111114": "123456789012345681",
            "111111111111111115": "123456789012345682",
            "111111111111111116": "123456789012345683",
            "111111111111111117": "123456789012345684",
            "111111111111111118": "123456789012345685",
        }
        for old_value, new_value in replacements.items():
            content = content.replace(old_value, new_value)
        config_path.write_text(content, encoding="utf-8")

    def test_runtime_timestamp_helpers_use_kst_date_and_sprint_id(self):
        boundary = datetime(2026, 3, 23, 15, 30, tzinfo=timezone.utc)

        self.assertTrue(new_request_id(boundary).startswith("20260324-"))
        self.assertTrue(new_backlog_id(boundary).startswith("backlog-20260324-"))
        self.assertTrue(new_todo_id(boundary).startswith("todo-003000-"))
        self.assertEqual(
            build_active_sprint_id(now=boundary),
            "260324-Sprint-00:30",
        )
        self.assertEqual(
            compute_next_slot_at(
                boundary,
                interval_minutes=180,
                timezone_name="Asia/Seoul",
            ).isoformat(),
            "2026-03-24T03:00:00+09:00",
        )

    def test_sprint_markdown_renders_single_sprint_id(self):
        sprint_state = {
            "sprint_id": "260324-Sprint-09:00",
            "sprint_name": "2026-03-24-active-sprint-id-compatibility",
            "phase": "ongoing",
            "milestone_title": "활성 스프린트 ID 출력/호환성 검증",
            "sprint_folder": "shared_workspace/sprints/260324-active-sprint-id-compatibility",
            "status": "running",
            "trigger": "backlog_ready",
            "started_at": "2026-03-24T09:00:00+09:00",
            "ended_at": "",
            "commit_sha": "",
            "selected_items": [],
            "todos": [],
        }

        current_markdown = render_current_sprint_markdown(sprint_state)
        history_markdown = render_sprint_history_markdown(sprint_state, "summary")

        self.assertIn("- sprint_id: 260324-Sprint-09:00", current_markdown)
        self.assertNotIn("sprint_series_id", current_markdown)
        self.assertNotIn("sprint_series_id", history_markdown)

    def test_sprint_markdown_keeps_legacy_sprint_id_without_series_line(self):
        sprint_state = {
            "sprint_id": "2026-Sprint-01-20260324T000000Z",
            "sprint_name": "legacy-active-sprint",
            "phase": "ongoing",
            "status": "running",
            "trigger": "backlog_ready",
            "started_at": "2026-03-24T00:00:00+09:00",
            "selected_items": [],
            "todos": [],
        }

        current_markdown = render_current_sprint_markdown(sprint_state)
        history_markdown = render_sprint_history_markdown(sprint_state, "summary")

        self.assertIn("- sprint_id: 2026-Sprint-01-20260324T000000Z", current_markdown)
        self.assertNotIn("sprint_series_id", current_markdown)
        self.assertNotIn("sprint_series_id", history_markdown)

    def test_sprint_markdown_renders_recent_activity_section(self):
        sprint_state = {
            "sprint_id": "260331-Sprint-08:47",
            "sprint_name": "debug-agent-activity",
            "phase": "ongoing",
            "status": "running",
            "trigger": "manual_start",
            "started_at": "2026-03-31T08:47:15+09:00",
            "selected_items": [],
            "todos": [],
            "recent_activity": [
                {
                    "timestamp": "2026-03-31T08:49:00+09:00",
                    "event_type": "role_started",
                    "role": "planner",
                    "status": "running",
                    "request_id": "20260331-req1",
                    "todo_id": "todo-1",
                    "summary": "planner 역할이 요청 처리를 시작했습니다.",
                    "details": "session=session-1",
                }
            ],
        }

        current_markdown = render_current_sprint_markdown(sprint_state)
        history_markdown = render_sprint_history_markdown(sprint_state, "summary")

        self.assertIn("## Recent Activity", current_markdown)
        self.assertIn("role=planner | event=role_started", current_markdown)
        self.assertIn("details=session=session-1", current_markdown)
        self.assertIn("## Recent Activity", history_markdown)
        self.assertIn("request_id=20260331-req1", history_markdown)

    def test_sprint_history_index_renders_milestone_column_and_loads_it(self):
        sprint_state = {
            "sprint_id": "260324-Sprint-09:00",
            "status": "completed",
            "milestone_title": "CLI status",
            "started_at": "2026-03-24T09:00:00+09:00",
            "ended_at": "2026-03-24T11:00:00+09:00",
            "commit_sha": "abc1234",
            "todos": [{}, {}],
        }
        rendered = render_sprint_history_index([], sprint_state)

        self.assertIn(
            "| sprint_id | status | milestone | started_at | ended_at | todo_count | commit_sha |",
            rendered,
        )
        self.assertIn(
            "| 260324-Sprint-09:00 | completed | CLI status | 2026-03-24T09:00:00+09:00 | 2026-03-24T11:00:00+09:00 | 2 | abc1234 |",
            rendered,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "index.md"
            index_path.write_text(rendered, encoding="utf-8")
            loaded_rows = load_sprint_history_index(index_path)

        self.assertEqual(len(loaded_rows), 1)
        self.assertEqual(loaded_rows[0]["milestone_title"], "CLI status")
        self.assertEqual(loaded_rows[0]["todo_count"], 2)

    def test_load_sprint_history_index_supports_legacy_rows_without_milestone_column(self):
        legacy_index = """# Sprint History Index

| sprint_id | status | started_at | ended_at | todo_count | commit_sha |
| --- | --- | --- | --- | ---: | --- |
| 260101-Sprint-09:00 | completed | 2026-01-01T09:00:00+09:00 | 2026-01-01T10:00:00+09:00 | 1 | legacysha |
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "index.md"
            index_path.write_text(legacy_index, encoding="utf-8")
            loaded_rows = load_sprint_history_index(index_path)

        self.assertEqual(len(loaded_rows), 1)
        self.assertEqual(loaded_rows[0]["sprint_id"], "260101-Sprint-09:00")
        self.assertEqual(loaded_rows[0]["milestone_title"], "")
        self.assertEqual(loaded_rows[0]["todo_count"], 1)

    def test_scaffold_workspace_and_load_configs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            created = scaffold_workspace(tmpdir)
            workspace_root = Path(tmpdir)

            self.assertGreater(len(created), 10)
            self.assertTrue((workspace_root / "discord_agents_config.yaml").exists())
            self.assertTrue((workspace_root / "team_runtime.yaml").exists())
            self.assertFalse((workspace_root / "docs").exists())

            discord_config = load_discord_agents_config(tmpdir)
            runtime_config = load_team_runtime_config(tmpdir)

            self.assertEqual(discord_config.relay_channel_id, "111111111111111111")
            self.assertEqual(discord_config.startup_channel_id, "111111111111111111")
            self.assertEqual(discord_config.report_channel_id, "111111111111111111")
            self.assertEqual(discord_config.agents["developer"].bot_id, "111111111111111116")
            self.assertEqual(discord_config.agents["qa"].bot_id, "111111111111111117")
            self.assertEqual(discord_config.get_internal_agent("sourcer").token_env, "AGENT_DISCORD_TOKEN_CS_ADMIN")
            self.assertEqual(discord_config.get_internal_agent("sourcer").bot_id, "111111111111111118")
            self.assertEqual(runtime_config.sprint_id, "2026-Sprint-01")
            self.assertEqual(runtime_config.sprint_interval_minutes, 180)
            self.assertEqual(runtime_config.sprint_timezone, "Asia/Seoul")
            self.assertEqual(runtime_config.sprint_mode, "hybrid")
            self.assertEqual(runtime_config.sprint_start_mode, "auto")
            self.assertEqual(runtime_config.sprint_cutoff_time, "22:00")
            self.assertEqual(runtime_config.sprint_overlap_policy, "no_overlap")
            self.assertEqual(runtime_config.sprint_ingress_mode, "backlog_first")
            self.assertEqual(runtime_config.sprint_discovery_scope, "broad_scan")
            self.assertEqual(runtime_config.sprint_discovery_actions, ())
            self.assertEqual(runtime_config.role_defaults["developer"].reasoning, "xhigh")
            self.assertEqual(runtime_config.role_defaults["qa"].reasoning, "medium")
            team_runtime_text = (workspace_root / "team_runtime.yaml").read_text(encoding="utf-8")
            communication_protocol = (workspace_root / "communication_protocol.md").read_text(encoding="utf-8")
            teams_runtime_skill = (
                workspace_root / ".agents" / "skills" / "teams-runtime" / "SKILL.md"
            ).read_text(encoding="utf-8")
            teams_runtime_openai_yaml = (
                workspace_root / ".agents" / "skills" / "teams-runtime" / "agents" / "openai.yaml"
            ).read_text(encoding="utf-8")
            teams_runtime_snapshot_script = (
                workspace_root
                / ".agents"
                / "skills"
                / "teams-runtime"
                / "scripts"
                / "collect_runtime_snapshot.py"
            ).read_text(encoding="utf-8")
            orchestrator_prompt = (workspace_root / "orchestrator" / "AGENTS.md").read_text(encoding="utf-8")
            orchestrator_sprint_skill = (
                workspace_root
                / "orchestrator"
                / ".agents"
                / "skills"
                / "sprint_orchestration"
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            orchestrator_agent_utilization_skill = (
                workspace_root
                / "orchestrator"
                / ".agents"
                / "skills"
                / "agent_utilization"
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            orchestrator_agent_utilization_policy = (
                workspace_root
                / "orchestrator"
                / ".agents"
                / "skills"
                / "agent_utilization"
                / "policy.yaml"
            ).read_text(encoding="utf-8")
            orchestrator_handoff_skill = (
                workspace_root
                / "orchestrator"
                / ".agents"
                / "skills"
                / "handoff_merging"
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            orchestrator_status_skill = (
                workspace_root
                / "orchestrator"
                / ".agents"
                / "skills"
                / "status_reporting"
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            orchestrator_closeout_skill = (
                workspace_root
                / "orchestrator"
                / ".agents"
                / "skills"
                / "sprint_closeout"
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            planner_prompt = (workspace_root / "planner" / "AGENTS.md").read_text(encoding="utf-8")
            architect_prompt = (workspace_root / "architect" / "AGENTS.md").read_text(encoding="utf-8")
            developer_prompt = (workspace_root / "developer" / "AGENTS.md").read_text(encoding="utf-8")
            planner_skill = (
                workspace_root
                / "planner"
                / ".agents"
                / "skills"
                / "documentation"
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            planner_management_skill = (
                workspace_root
                / "planner"
                / ".agents"
                / "skills"
                / "backlog_management"
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            planner_backlog_skill = (
                workspace_root
                / "planner"
                / ".agents"
                / "skills"
                / "backlog_decomposition"
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            planner_sprint_skill = (
                workspace_root
                / "planner"
                / ".agents"
                / "skills"
                / "sprint_planning"
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            version_controller_prompt = (
                workspace_root / "internal" / "version_controller" / "AGENTS.md"
            ).read_text(encoding="utf-8")
            version_controller_skill = (
                workspace_root
                / "internal"
                / "version_controller"
                / ".agents"
                / "skills"
                / "version_controller"
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            commit_policy = (workspace_root / "COMMIT_POLICY.md").read_text(encoding="utf-8")
            self.assertIn("name: teams-runtime", teams_runtime_skill)
            self.assertIn("`teams`, `팀즈`, or `팀`", teams_runtime_skill)
            self.assertIn("Use this skill to operate a generated `teams_runtime` workspace", teams_runtime_skill)
            self.assertIn("python -m teams_runtime list --workspace-root .", teams_runtime_skill)
            self.assertIn("Do not edit `.teams_runtime/*.json` directly", teams_runtime_skill)
            self.assertIn("display_name: \"teams_runtime Operator\"", teams_runtime_openai_yaml)
            self.assertIn("Use $teams-runtime", teams_runtime_openai_yaml)
            self.assertIn("Collect a compact read-only teams_runtime snapshot.", teams_runtime_snapshot_script)
            self.assertIn("\"teams_runtime\", \"list\"", teams_runtime_snapshot_script)
            self.assertIn("--include-ps", teams_runtime_snapshot_script)
            self.assertIn("--log-role", teams_runtime_snapshot_script)
            self.assertIn("planner에 먼저 위임한다", orchestrator_prompt)
            self.assertIn("기존 planner request를 재사용한다", orchestrator_prompt)
            self.assertIn("version_controller", orchestrator_prompt)
            self.assertIn("./.agents/skills/", orchestrator_prompt)
            self.assertIn("사용 가능한 skill", orchestrator_prompt)
            self.assertIn("planning과 backlog management 책임은 planner", orchestrator_prompt)
            self.assertIn("planner가 직접 `.teams_runtime/backlog`", orchestrator_prompt)
            self.assertIn("workflow governor", orchestrator_prompt)
            self.assertIn("각 agent의 역할, skill, 강점, 행동 특성", orchestrator_prompt)
            self.assertIn("./.agents/skills/agent_utilization/policy.yaml", orchestrator_prompt)
            self.assertIn("shared_workspace/sprints/<sprint_folder_name>/", orchestrator_prompt)
            self.assertNotIn("{ORCHESTRATOR_CAPABILITY_REFERENCE}", orchestrator_prompt)
            self.assertIn("sprint-state status mutations", orchestrator_prompt)
            self.assertIn("모든 사용자 요청은 parser 없이 orchestrator agent가 먼저 받아 해석하고 처리한다", orchestrator_prompt)
            self.assertIn("status/cancel/sprint control/action 실행 같은 운영 요청도 먼저 orchestrator agent가 skill과 persisted state를 읽고 판단한다", orchestrator_prompt)
            self.assertIn("python -m teams_runtime sprint start|stop|restart|status", orchestrator_prompt)
            self.assertIn("legacy `approve request_id:...`", orchestrator_prompt)
            self.assertNotIn("승인 관리", orchestrator_prompt)
            self.assertIn("코드베이스와 모듈 구조를 overview", architect_prompt)
            self.assertIn("task별 technical specification", architect_prompt)
            self.assertIn("developer 결과를 검토해", architect_prompt)
            self.assertIn("name: sprint_orchestration", orchestrator_sprint_skill)
            self.assertIn("Use the lifecycle surface.", orchestrator_sprint_skill)
            self.assertIn("python -m teams_runtime sprint start --workspace-root <team_workspace_root> --milestone", orchestrator_sprint_skill)
            self.assertIn("--brief", orchestrator_sprint_skill)
            self.assertIn("--artifact", orchestrator_sprint_skill)
            self.assertIn("Do not manually rewrite sprint JSON", orchestrator_sprint_skill)
            self.assertIn("name: agent_utilization", orchestrator_agent_utilization_skill)
            self.assertIn("role, skills, strengths, and behavior", orchestrator_agent_utilization_skill)
            self.assertIn("sibling `policy.yaml`", orchestrator_agent_utilization_skill)
            self.assertIn("machine-readable routing and scoring authority", orchestrator_agent_utilization_skill)
            self.assertIn("user_intake", orchestrator_agent_utilization_skill)
            self.assertIn("weights:", orchestrator_agent_utilization_policy)
            self.assertIn("user_intake: planner", orchestrator_agent_utilization_policy)
            self.assertIn("sourcer_review: planner", orchestrator_agent_utilization_policy)
            self.assertIn("planning_resume: planner", orchestrator_agent_utilization_policy)
            self.assertIn("sprint_initial_default: planner", orchestrator_agent_utilization_policy)
            self.assertIn("planner_reentry_requires_explicit_signal: true", orchestrator_agent_utilization_policy)
            self.assertIn("verification_result_terminal: true", orchestrator_agent_utilization_policy)
            self.assertIn("ignore_non_planner_backlog_proposals_for_routing: true", orchestrator_agent_utilization_policy)
            self.assertIn("public_roles:", orchestrator_agent_utilization_policy)
            self.assertIn("preferred_skill_signal", orchestrator_agent_utilization_policy)
            self.assertIn("name: handoff_merging", orchestrator_handoff_skill)
            self.assertIn("planner-owned persistence", orchestrator_handoff_skill)
            self.assertIn("name: status_reporting", orchestrator_status_skill)
            self.assertIn("Do not use this skill for sprint lifecycle commands or sprint status", orchestrator_status_skill)
            self.assertIn("name: sprint_closeout", orchestrator_closeout_skill)
            self.assertIn("./.agents/skills/", planner_prompt)
            self.assertIn("사용 가능한 skill", planner_prompt)
            self.assertIn("backlog 관리", planner_prompt)
            self.assertIn("planner가 직접 `.teams_runtime/backlog/*.json`", planner_prompt)
            self.assertIn("shared_workspace/sprints/<sprint_folder_name>/", planner_prompt)
            self.assertIn("Current request.artifacts`는 planning reference input으로 취급한다", planner_prompt)
            self.assertIn("shared_workspace/sprint_history/", planner_prompt)
            self.assertIn("shared_workspace/sprint_history/index.md", planner_prompt)
            self.assertIn("historical context로 확인한다", planner_prompt)
            self.assertIn("현재 request, active sprint 문서, kickoff context를 덮어쓰지 않는다", planner_prompt)
            self.assertIn("첨부 경로가 존재하지만 현재 세션에서 직접 읽을 수 없는 형식이면", planner_prompt)
            self.assertIn("원본 kickoff brief/requirements를 보존한 채", planner_prompt)
            self.assertIn("sprint backlog/todo 개수를 3건으로 고정하지 않는다", planner_prompt)
            self.assertIn("independently reviewable implementation slice", planner_prompt)
            self.assertIn("여러 subsystem, contract, phase, deliverable", planner_prompt)
            self.assertIn("planner history나 shared planning log", planner_prompt)
            self.assertIn("Current request.params._teams_kind == \"blocked_backlog_review\"", planner_prompt)
            self.assertIn("blocked 유지", planner_prompt)
            self.assertIn("재개 판단이 난 항목만 `pending`으로 되돌리고", planner_prompt)
            self.assertNotIn("커밋 메시지는", developer_prompt)
            self.assertIn("version_controller가 task 완료 시점 커밋을 수행", developer_prompt)
            self.assertIn("./.agents/skills/", version_controller_prompt)
            self.assertIn("사용 가능한 skill", version_controller_prompt)
            self.assertIn(
                "active sprint milestone이 있으면 그 milestone을 직접 진전시키는 backlog 후보에 집중한다",
                (Path(tmpdir) / "internal" / "sourcer" / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertIn("name: documentation", planner_skill)
            self.assertIn("update the real `.md` file in the workspace", planner_skill)
            self.assertIn("reading sprint attachment documents passed through `Current request.artifacts`", planner_skill)
            self.assertIn("kickoff.md", planner_skill)
            self.assertIn("shared_workspace/sprint_history/index.md", planner_skill)
            self.assertIn("keep the current request, active sprint docs, and kickoff context authoritative", planner_skill)
            self.assertIn("Do not silently ignore referenced attachments", planner_skill)
            self.assertIn("Do not bulk-read `shared_workspace/sprint_history/`", planner_skill)
            self.assertIn("Do not directly edit `shared_workspace/backlog.md`", planner_skill)
            self.assertIn("single independently reviewable implementation slice", planner_skill)
            self.assertIn("prior planner history or shared planning logs", planner_skill)
            self.assertIn("name: backlog_management", planner_management_skill)
            self.assertIn("python -m teams_runtime.core.backlog_store merge", planner_management_skill)
            self.assertIn("name: backlog_decomposition", planner_backlog_skill)
            self.assertIn("a sprint milestone", planner_backlog_skill)
            self.assertIn("single sprint milestone's backlog breakdown", planner_backlog_skill)
            self.assertIn("single independently reviewable implementation slice", planner_backlog_skill)
            self.assertIn("multiple subsystems, contracts, phases, deliverables", planner_backlog_skill)
            self.assertIn("Do not force the decomposition into three items", planner_backlog_skill)
            self.assertIn("name: sprint_planning", planner_sprint_skill)
            self.assertIn("immutable kickoff brief", planner_sprint_skill)
            self.assertIn("shared_workspace/sprint_history/index.md", planner_sprint_skill)
            self.assertIn("already-closed decisions", planner_sprint_skill)
            self.assertIn("keep the current request, active sprint docs, and kickoff context authoritative", planner_sprint_skill)
            self.assertIn("sprint's single `milestone_title`", planner_sprint_skill)
            self.assertIn("sprint inclusion must be milestone-relevant", planner_sprint_skill)
            self.assertIn("Do not default to three promoted items", planner_sprint_skill)
            self.assertIn("single independently reviewable implementation slice", planner_sprint_skill)
            self.assertIn("More than three promoted items is normal", planner_sprint_skill)
            self.assertIn("Reopen blocked backlog explicitly.", planner_sprint_skill)
            self.assertIn("Do not move a `blocked` item directly into sprint selection", planner_sprint_skill)
            self.assertIn("Do not bulk-read `shared_workspace/sprint_history/`", planner_sprint_skill)
            self.assertIn("helper command", version_controller_prompt)
            self.assertIn("name: version_controller", version_controller_skill)
            self.assertIn("The internal version_controller agent is the primary owner", commit_policy)
            self.assertTrue((workspace_root / "qa" / "AGENTS.md").exists())
            self.assertNotIn("approval:", team_runtime_text)
            self.assertNotIn("auto_implement", team_runtime_text)
            self.assertNotIn("high_risk_requires_approval", team_runtime_text)
            self.assertNotIn("|status|approve|cancel|", communication_protocol)
            self.assertNotIn("approval pauses", (workspace_root / "file_contracts.md").read_text(encoding="utf-8"))

    def test_scaffold_workspace_planner_prompts_reference_prior_sprint_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            workspace_root = Path(tmpdir)
            planner_prompt = (workspace_root / "planner" / "AGENTS.md").read_text(encoding="utf-8")
            planner_skill = (
                workspace_root / "planner" / ".agents" / "skills" / "documentation" / "SKILL.md"
            ).read_text(encoding="utf-8")
            planner_sprint_skill = (
                workspace_root / "planner" / ".agents" / "skills" / "sprint_planning" / "SKILL.md"
            ).read_text(encoding="utf-8")

            self.assertIn("shared_workspace/sprint_history/", planner_prompt)
            self.assertIn("shared_workspace/sprint_history/index.md", planner_prompt)
            self.assertIn("historical context로 확인한다", planner_prompt)
            self.assertIn("현재 request, active sprint 문서, kickoff context를 덮어쓰지 않는다", planner_prompt)
            self.assertIn("shared_workspace/sprint_history/index.md", planner_skill)
            self.assertIn(
                "keep the current request, active sprint docs, and kickoff context authoritative",
                planner_skill,
            )
            self.assertIn("Do not bulk-read `shared_workspace/sprint_history/`", planner_skill)
            self.assertIn("shared_workspace/sprint_history/index.md", planner_sprint_skill)
            self.assertIn("already-closed decisions", planner_sprint_skill)
            self.assertIn(
                "keep the current request, active sprint docs, and kickoff context authoritative",
                planner_sprint_skill,
            )
            self.assertIn("Do not bulk-read `shared_workspace/sprint_history/`", planner_sprint_skill)

    def test_load_team_runtime_config_rejects_legacy_approval_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_path.write_text(
                """sprint:\n  id: \"2026-Sprint-01\"\napproval:\n  auto_implement: true\n""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "approval is no longer supported"):
                load_team_runtime_config(tmpdir)

    def test_load_team_runtime_config_rejects_action_approval_required(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_path.write_text(
                """sprint:\n  id: \"2026-Sprint-01\"\nactions:\n  run_test:\n    command: [\"python\", \"-m\", \"pytest\"]\n    lifecycle: \"foreground\"\n    domain: \"개발\"\n    approval_required: false\n""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "approval_required is no longer supported"):
                load_team_runtime_config(tmpdir)
            self.assertTrue((Path(tmpdir) / "internal" / "parser" / "AGENTS.md").exists())
            self.assertTrue((Path(tmpdir) / "internal" / "sourcer" / "AGENTS.md").exists())
            self.assertTrue((Path(tmpdir) / "internal" / "version_controller" / "AGENTS.md").exists())
            self.assertTrue((Path(tmpdir) / "shared_workspace" / "backlog.md").exists())
            self.assertTrue((Path(tmpdir) / "shared_workspace" / "completed_backlog.md").exists())

    def test_load_agent_utilization_policy_falls_back_when_skill_policy_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            policy_path = (
                Path(tmpdir)
                / "orchestrator"
                / ".agents"
                / "skills"
                / "agent_utilization"
                / "policy.yaml"
            )
            policy_path.unlink()

            policy = load_agent_utilization_policy(tmpdir)

            self.assertEqual(policy.policy_source, "default_fallback")
            self.assertIn("Missing skill policy file", policy.load_error)
            self.assertEqual(policy.user_intake_role, "planner")
            self.assertEqual(policy.sourcer_review_role, "planner")
            self.assertEqual(policy.planning_resume_role, "planner")
            self.assertEqual(policy.sprint_initial_default_role, "planner")
            self.assertTrue(policy.planner_reentry_requires_explicit_signal)
            self.assertTrue(policy.verification_result_terminal)
            self.assertTrue(policy.ignore_non_planner_backlog_proposals_for_routing)
            self.assertTrue((Path(tmpdir) / "shared_workspace" / "current_sprint.md").exists())
            self.assertTrue((Path(tmpdir) / "shared_workspace" / "sprints" / "README.md").exists())
            self.assertTrue((Path(tmpdir) / "shared_workspace" / "sprint_history" / "index.md").exists())

    def test_load_team_runtime_config_supports_manual_daily_sprint_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            content = config_path.read_text(encoding="utf-8")
            content = content.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            content = content.replace('  cutoff_time: "22:00"\n', '  cutoff_time: "21:30"\n', 1)
            config_path.write_text(content, encoding="utf-8")

            runtime_config = load_team_runtime_config(tmpdir)

            self.assertEqual(runtime_config.sprint_start_mode, "manual_daily")
            self.assertEqual(runtime_config.sprint_cutoff_time, "21:30")

    def test_update_team_runtime_role_defaults_updates_model_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            updated = update_team_runtime_role_defaults(tmpdir, "developer", model="gpt-5.5")

            self.assertEqual(updated.model, "gpt-5.5")
            self.assertEqual(updated.reasoning, "xhigh")
            runtime_config = load_team_runtime_config(tmpdir)
            self.assertEqual(runtime_config.role_defaults["developer"].model, "gpt-5.5")
            self.assertEqual(runtime_config.role_defaults["developer"].reasoning, "xhigh")

    def test_update_team_runtime_role_defaults_updates_reasoning_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            updated = update_team_runtime_role_defaults(tmpdir, "planner", reasoning="low")

            self.assertEqual(updated.model, "gpt-5.4")
            self.assertEqual(updated.reasoning, "low")
            runtime_config = load_team_runtime_config(tmpdir)
            self.assertEqual(runtime_config.role_defaults["planner"].model, "gpt-5.4")
            self.assertEqual(runtime_config.role_defaults["planner"].reasoning, "low")

    def test_update_team_runtime_role_defaults_requires_at_least_one_field(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            with self.assertRaisesRegex(ValueError, "At least one of model or reasoning"):
                update_team_runtime_role_defaults(tmpdir, "planner")

    def test_load_discord_agents_config_supports_internal_agents_and_report_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            config_path.write_text(
                """relay_channel_id: "123456789012345678"
startup_channel_id: "123456789012345679"
report_channel_id: "123456789012345680"
agents:
  orchestrator:
    name: orchestrator
    role: orchestrator
    description: orchestrator
    token_env: AGENT_DISCORD_TOKEN_ORCHESTRATOR
    bot_id: "123456789012345681"
  planner:
    name: planner
    role: planner
    description: planner
    token_env: AGENT_DISCORD_TOKEN_PLANNER
    bot_id: "123456789012345682"
  designer:
    name: designer
    role: designer
    description: designer
    token_env: AGENT_DISCORD_TOKEN_DESIGNER
    bot_id: "123456789012345683"
  architect:
    name: architect
    role: architect
    description: architect
    token_env: AGENT_DISCORD_TOKEN_ARCHITECT
    bot_id: "123456789012345684"
  developer:
    name: developer
    role: developer
    description: developer
    token_env: AGENT_DISCORD_TOKEN_DEVELOPER
    bot_id: "123456789012345685"
  qa:
    name: qa
    role: qa
    description: qa
    token_env: AGENT_DISCORD_TOKEN_QA
    bot_id: "123456789012345686"
internal_agents:
  sourcer:
    name: CS_ADMIN
    role: sourcer
    description: sourcer reporter
    token_env: AGENT_DISCORD_TOKEN_CS_ADMIN
    bot_id: "123456789012345687"
""",
                encoding="utf-8",
            )

            discord_config = load_discord_agents_config(tmpdir)

            self.assertEqual(discord_config.report_channel_id, "123456789012345680")
            self.assertEqual(discord_config.get_internal_agent("sourcer").token_env, "AGENT_DISCORD_TOKEN_CS_ADMIN")
            self.assertEqual(discord_config.get_internal_agent("sourcer").bot_id, "123456789012345687")
            self.assertIn("123456789012345687", discord_config.trusted_bot_ids)

    def test_load_discord_agents_config_uses_startup_channel_defaulting_to_relay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            content = config_path.read_text(encoding="utf-8").replace(
                'startup_channel_id: "111111111111111111"\n',
                "",
                1,
            )
            config_path.write_text(content, encoding="utf-8")

            discord_config = load_discord_agents_config(tmpdir)
            self.assertEqual(discord_config.startup_channel_id, discord_config.relay_channel_id)

    def test_load_discord_agents_config_requires_bot_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            content = config_path.read_text(encoding="utf-8").replace(
                '    bot_id: "111111111111111113"\n',
                "",
                1,
            )
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "planner.bot_id"):
                load_discord_agents_config(tmpdir)

    def test_validate_runtime_discord_agents_config_rejects_scaffold_placeholders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            with self.assertRaisesRegex(ValueError, "placeholder snowflake"):
                validate_runtime_discord_agents_config(tmpdir)

    def test_validate_runtime_discord_agents_config_allows_placeholder_override_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            with patch.dict(os.environ, {"TEAMS_RUNTIME_ALLOW_PLACEHOLDER_IDS": "1"}, clear=False):
                config = validate_runtime_discord_agents_config(tmpdir)

            self.assertEqual(config.relay_channel_id, "111111111111111111")
            self.assertEqual(config.config_path, str((Path(tmpdir).resolve() / "discord_agents_config.yaml")))

    def test_cli_defaults_workspace_root_to_teams_generated_from_project_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir).resolve()
            workspace_root = project_root / DEFAULT_WORKSPACE_DIRNAME
            scaffold_workspace(workspace_root)
            current = Path.cwd()
            try:
                os.chdir(project_root)
                self.assertEqual(resolve_workspace_root(None), workspace_root)
                self.assertEqual(main(["status"]), 0)
            finally:
                os.chdir(current)

    def test_cli_defaults_workspace_root_to_workspace_teams_generated_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir).resolve()
            workspace_root = project_root / "workspace" / DEFAULT_WORKSPACE_DIRNAME
            scaffold_workspace(workspace_root)
            current = Path.cwd()
            try:
                os.chdir(project_root)
                self.assertEqual(resolve_workspace_root(None), workspace_root)
            finally:
                os.chdir(current)

    def test_cli_uses_current_directory_when_already_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir).resolve() / DEFAULT_WORKSPACE_DIRNAME
            scaffold_workspace(workspace_root)

            current = Path.cwd()
            try:
                os.chdir(workspace_root)
                self.assertTrue(is_workspace_root(Path.cwd()))
                self.assertEqual(resolve_workspace_root(None), workspace_root)
            finally:
                os.chdir(current)

    def test_scaffold_workspace_does_not_generate_docs_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            self.assertFalse((Path(tmpdir) / "docs").exists())

    def test_scaffold_workspace_preserves_existing_discord_agents_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            config_path = workspace_root / "discord_agents_config.yaml"
            custom_content = """relay_channel_id: "222222222222222222"
startup_channel_id: "222222222222222222"
agents:
  orchestrator:
    name: orchestrator
    role: orchestrator
    description: custom
    token_env: AGENT_DISCORD_TOKEN_ORCHESTRATOR
    bot_id: "222222222222222223"
  planner:
    name: planner
    role: planner
    description: custom
    token_env: AGENT_DISCORD_TOKEN_PLANNER
    bot_id: "222222222222222224"
  designer:
    name: designer
    role: designer
    description: custom
    token_env: AGENT_DISCORD_TOKEN_DESIGNER
    bot_id: "222222222222222225"
  architect:
    name: architect
    role: architect
    description: custom
    token_env: AGENT_DISCORD_TOKEN_ARCHITECT
    bot_id: "222222222222222226"
  developer:
    name: developer
    role: developer
    description: custom
    token_env: AGENT_DISCORD_TOKEN_DEVELOPER
    bot_id: "222222222222222227"
  qa:
    name: qa
    role: qa
    description: custom
    token_env: AGENT_DISCORD_TOKEN_QA
    bot_id: "222222222222222228"
"""
            config_path.write_text(custom_content, encoding="utf-8")

            created = scaffold_workspace(workspace_root)

            self.assertTrue((workspace_root / "team_runtime.yaml").exists())
            self.assertTrue((workspace_root / "planner" / "AGENTS.md").exists())
            self.assertNotIn(config_path, created)
            self.assertEqual(config_path.read_text(encoding="utf-8"), custom_content)
            discord_config = load_discord_agents_config(workspace_root)
            self.assertEqual(discord_config.relay_channel_id, "222222222222222222")
            self.assertEqual(discord_config.agents["developer"].bot_id, "222222222222222227")

    def test_scaffold_workspace_reinitializes_generated_files_from_scratch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            config_path = workspace_root / "discord_agents_config.yaml"
            custom_content = """relay_channel_id: "333333333333333333"
startup_channel_id: "333333333333333333"
agents:
  orchestrator:
    name: orchestrator
    role: orchestrator
    description: custom
    token_env: AGENT_DISCORD_TOKEN_ORCHESTRATOR
    bot_id: "333333333333333334"
  planner:
    name: planner
    role: planner
    description: custom
    token_env: AGENT_DISCORD_TOKEN_PLANNER
    bot_id: "333333333333333335"
  designer:
    name: designer
    role: designer
    description: custom
    token_env: AGENT_DISCORD_TOKEN_DESIGNER
    bot_id: "333333333333333336"
  architect:
    name: architect
    role: architect
    description: custom
    token_env: AGENT_DISCORD_TOKEN_ARCHITECT
    bot_id: "333333333333333337"
  developer:
    name: developer
    role: developer
    description: custom
    token_env: AGENT_DISCORD_TOKEN_DEVELOPER
    bot_id: "333333333333333338"
  qa:
    name: qa
    role: qa
    description: custom
    token_env: AGENT_DISCORD_TOKEN_QA
    bot_id: "333333333333333339"
"""
            config_path.write_text(custom_content, encoding="utf-8")
            scaffold_workspace(workspace_root)

            (workspace_root / "team_runtime.yaml").write_text("sprint:\n  id: old\n", encoding="utf-8")
            (workspace_root / "planner" / "history.md").write_text("stale history", encoding="utf-8")
            (workspace_root / "shared_workspace" / "current_sprint.md").write_text("stale sprint", encoding="utf-8")
            (workspace_root / "logs" / "agents").mkdir(parents=True, exist_ok=True)
            (workspace_root / "logs" / "agents" / "planner.log").write_text("old log", encoding="utf-8")
            (workspace_root / ".teams_runtime").mkdir(parents=True, exist_ok=True)
            (workspace_root / ".teams_runtime" / "stale.json").write_text("{}", encoding="utf-8")
            (workspace_root / ".agents" / "skills" / "teams-runtime" / "SKILL.md").write_text(
                "stale skill",
                encoding="utf-8",
            )
            (workspace_root / ".agents" / "skills" / "teams-runtime" / "stale.txt").write_text(
                "stale artifact",
                encoding="utf-8",
            )

            created = scaffold_workspace(workspace_root)

            self.assertNotIn(config_path, created)
            self.assertEqual(config_path.read_text(encoding="utf-8"), custom_content)
            self.assertIn('id: "2026-Sprint-01"', (workspace_root / "team_runtime.yaml").read_text(encoding="utf-8"))
            self.assertEqual((workspace_root / "planner" / "history.md").read_text(encoding="utf-8"), "# Planner History\n")
            self.assertIn("# Current Sprint", (workspace_root / "shared_workspace" / "current_sprint.md").read_text(encoding="utf-8"))
            self.assertFalse((workspace_root / "logs" / "agents" / "planner.log").exists())
            self.assertFalse((workspace_root / ".teams_runtime" / "stale.json").exists())
            self.assertEqual(
                (workspace_root / ".agents" / "skills" / "teams-runtime" / "SKILL.md").read_text(encoding="utf-8").splitlines()[1],
                "name: teams-runtime",
            )
            self.assertFalse((workspace_root / ".agents" / "skills" / "teams-runtime" / "stale.txt").exists())

    def test_scaffold_workspace_preserves_sprint_history_and_rebuilds_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            sprint_id = "260324-Sprint-09:00"
            sprint_folder = workspace_root / "shared_workspace" / "sprints" / "260324-Sprint-09-00"
            sprint_folder.mkdir(parents=True, exist_ok=True)
            (sprint_folder / "report.md").write_text("preserved report", encoding="utf-8")
            history_file = workspace_root / "shared_workspace" / "sprint_history" / f"{sprint_id}.md"
            history_file.write_text(
                "# Sprint History\n\n"
                f"- sprint_id: {sprint_id}\n"
                "- sprint_name: preserved-history\n"
                "- phase: closeout\n"
                "- requested_milestone_title: Requested milestone\n"
                "- milestone_title: Preserved milestone\n"
                "- sprint_folder: shared_workspace/sprints/260324-Sprint-09-00\n"
                "- status: completed\n"
                "- trigger: manual_start\n"
                "- started_at: 2026-03-24T09:00:00+09:00\n"
                "- ended_at: 2026-03-24T11:00:00+09:00\n"
                "- commit_sha: abc1234\n\n"
                "## Todo List\n\n"
                "### 첫 번째 할 일\n"
                "- status: completed\n\n"
                "### 두 번째 할 일\n"
                "- status: completed\n\n"
                "## Recent Activity\n\n"
                "- recent activity 없음\n\n"
                "## Sprint Report\n\n"
                "preserved report\n",
                encoding="utf-8",
            )
            (workspace_root / "shared_workspace" / "sprint_history" / "index.md").write_text(
                "# Sprint History Index\n\n"
                "| sprint_id | status | started_at | ended_at | todo_count | commit_sha |\n"
                "| --- | --- | --- | --- | ---: | --- |\n"
                f"| {sprint_id} | completed | 2026-03-24T09:00:00+09:00 | 2026-03-24T11:00:00+09:00 | 2 | abc1234 |\n",
                encoding="utf-8",
            )
            write_json(paths.sprint_file(sprint_id), {"sprint_id": sprint_id, "status": "completed"})
            (workspace_root / ".teams_runtime" / "stale.json").write_text("{}", encoding="utf-8")

            scaffold_workspace(workspace_root)

            self.assertTrue(history_file.exists())
            self.assertIn("milestone_title: Preserved milestone", history_file.read_text(encoding="utf-8"))
            self.assertFalse((sprint_folder / "report.md").exists())
            self.assertFalse(paths.sprint_file(sprint_id).exists())
            self.assertFalse((workspace_root / ".teams_runtime" / "stale.json").exists())
            self.assertIn(
                "# Current Sprint",
                (workspace_root / "shared_workspace" / "current_sprint.md").read_text(encoding="utf-8"),
            )
            rendered_index = (workspace_root / "shared_workspace" / "sprint_history" / "index.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "| sprint_id | status | milestone | started_at | ended_at | todo_count | commit_sha |",
                rendered_index,
            )
            self.assertIn("Preserved milestone", rendered_index)

    def test_background_start_env_includes_package_parent_on_pythonpath(self):
        env = build_background_env()
        pythonpath_entries = [item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item]
        self.assertIn(str(Path(__file__).resolve().parents[2]), pythonpath_entries)

    def test_runtime_paths_place_agent_logs_under_workspace_logs_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            paths.ensure_runtime_dirs()
            root = Path(tmpdir).resolve()

            self.assertEqual(paths.agent_runtime_log("planner"), root / "logs" / "agents" / "planner.log")
            self.assertEqual(paths.agent_log_archive_dir, root / "logs" / "agents" / "archive")
            self.assertEqual(paths.agent_discord_log("planner"), root / "logs" / "discord" / "planner.jsonl")
            self.assertEqual(
                paths.operation_log_file("op-1"),
                root / "logs" / "operations" / "op-1.log",
            )
            self.assertEqual(paths.backlog_dir, root / ".teams_runtime" / "backlog")
            self.assertEqual(paths.sprints_dir, root / ".teams_runtime" / "sprints")
            self.assertEqual(paths.sprint_scheduler_file, root / ".teams_runtime" / "sprint_scheduler.json")
            self.assertEqual(paths.shared_backlog_file, root / "shared_workspace" / "backlog.md")
            self.assertEqual(
                paths.shared_completed_backlog_file,
                root / "shared_workspace" / "completed_backlog.md",
            )
            self.assertEqual(paths.current_sprint_file, root / "shared_workspace" / "current_sprint.md")
            self.assertEqual(
                paths.sprint_attachment_root("260404-Sprint-12-00"),
                root / "shared_workspace" / "sprints" / "260404-Sprint-12-00" / "attachments",
            )

    def test_cmd_start_preflights_service_configuration_before_spawning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            self._write_runtime_ready_discord_config(tmpdir)
            with patch("teams_runtime.cli.TeamService", side_effect=RuntimeError("preflight failed")):
                with patch("teams_runtime.cli.start_background_role_service") as start_mock:
                    with self.assertRaisesRegex(RuntimeError, "preflight failed"):
                        cmd_start(Path(tmpdir), None)
                    start_mock.assert_not_called()

    def test_cmd_config_role_set_updates_runtime_yaml_without_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            output = io.StringIO()
            with patch("teams_runtime.cli.cmd_restart") as restart_mock:
                with redirect_stdout(output):
                    exit_code = cmd_config_role_set(Path(tmpdir), "developer", model="gpt-5.5", reasoning="low")

            self.assertEqual(exit_code, 0)
            restart_mock.assert_not_called()
            runtime_config = load_team_runtime_config(tmpdir)
            self.assertEqual(runtime_config.role_defaults["developer"].model, "gpt-5.5")
            self.assertEqual(runtime_config.role_defaults["developer"].reasoning, "low")
            rendered = output.getvalue()
            self.assertIn("Updated", rendered)
            self.assertIn("role=developer model=gpt-5.5 reasoning=low", rendered)
            self.assertIn("python -m teams_runtime restart --agent developer", rendered)

    def test_cmd_start_supports_internal_parser_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            self._write_runtime_ready_discord_config(tmpdir)
            with patch("teams_runtime.cli.start_background_role_service", return_value=42424) as start_mock:
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = cmd_start(Path(tmpdir), "parser")

            self.assertEqual(exit_code, 0)
            self.assertEqual(start_mock.call_count, 1)
            self.assertEqual(start_mock.call_args.args[1], "parser")
            self.assertEqual(start_mock.call_args.kwargs.get("relay_transport"), "internal")
            self.assertIn("Started parser service in background", output.getvalue())

    def test_cmd_start_supports_discord_relay_transport_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            self._write_runtime_ready_discord_config(tmpdir)
            with patch("teams_runtime.cli.start_background_role_service", return_value=42424) as start_mock:
                cmd_start(Path(tmpdir), "planner", relay_transport="discord")

            self.assertEqual(start_mock.call_args.args[1], "planner")
            self.assertEqual(start_mock.call_args.kwargs.get("relay_transport"), "discord")

    def test_build_background_command_includes_relay_transport_argument(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            command = build_background_command(Path(tmpdir), "planner")
            self.assertEqual(command[-2:], ["--relay-transport", "internal"])
            debug_command = build_background_command(Path(tmpdir), "planner", relay_transport="discord")
            self.assertEqual(debug_command[-2:], ["--relay-transport", "discord"])

    def test_main_run_passes_relay_transport_to_run_services(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            self._write_runtime_ready_discord_config(tmpdir)
            with patch("teams_runtime.cli.run_services", new=AsyncMock(return_value=None)) as run_services_mock:
                exit_code = main(
                    [
                        "run",
                        "--workspace-root",
                        tmpdir,
                        "--agent",
                        "planner",
                        "--relay-transport",
                        "discord",
                    ]
                )

            self.assertEqual(exit_code, 0)
            run_services_mock.assert_awaited_once()
            self.assertEqual(run_services_mock.call_args.kwargs.get("relay_transport"), "discord")

    def test_cmd_sprint_start_uses_orchestrator_service_without_discord_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            service = SimpleNamespace(
                start_sprint_lifecycle=AsyncMock(return_value="manual sprint initial phase를 시작했습니다.")
            )
            with patch("teams_runtime.cli.TeamService", return_value=service) as service_mock:
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = cmd_sprint_start(Path(tmpdir), "workflow initial")

            self.assertEqual(exit_code, 0)
            service_mock.assert_called_once_with(Path(tmpdir), "orchestrator", enable_discord_client=False)
            service.start_sprint_lifecycle.assert_awaited_once_with(
                "workflow initial",
                trigger="manual_start",
                resume_mode="await",
                kickoff_brief="",
                kickoff_requirements=[],
                kickoff_request_text="start sprint\nmilestone: workflow initial",
                kickoff_source_request_id="",
                kickoff_reference_artifacts=[],
            )
            self.assertIn("manual sprint initial phase를 시작했습니다.", output.getvalue())

    def test_cmd_sprint_start_forwards_kickoff_brief_requirements_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            service = SimpleNamespace(
                start_sprint_lifecycle=AsyncMock(return_value="manual sprint initial phase를 시작했습니다.")
            )
            with patch("teams_runtime.cli.TeamService", return_value=service):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = cmd_sprint_start(
                        Path(tmpdir),
                        "workflow initial",
                        brief="preserve kickoff detail",
                        requirements=["keep original brief", "derive refined title separately"],
                        artifacts=["./shared_workspace/sprints/260404-Sprint-09-00/attachments/att-1_scope.md"],
                        source_request_id="request-origin-1",
                    )

            self.assertEqual(exit_code, 0)
            service.start_sprint_lifecycle.assert_awaited_once_with(
                "workflow initial",
                trigger="manual_start",
                resume_mode="await",
                kickoff_brief="preserve kickoff detail",
                kickoff_requirements=["keep original brief", "derive refined title separately"],
                kickoff_request_text=(
                    "start sprint\n"
                    "milestone: workflow initial\n"
                    "brief:\n"
                    "preserve kickoff detail\n"
                    "requirements:\n"
                    "- keep original brief\n"
                    "- derive refined title separately"
                ),
                kickoff_source_request_id="request-origin-1",
                kickoff_reference_artifacts=["./shared_workspace/sprints/260404-Sprint-09-00/attachments/att-1_scope.md"],
            )

    def test_cmd_sprint_stop_and_restart_use_orchestrator_service_without_discord_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            stop_service = SimpleNamespace(stop_sprint_lifecycle=AsyncMock(return_value="stop requested"))
            restart_service = SimpleNamespace(restart_sprint_lifecycle=AsyncMock(return_value="restart requested"))
            with patch("teams_runtime.cli.TeamService", side_effect=[stop_service, restart_service]) as service_mock:
                stop_output = io.StringIO()
                with redirect_stdout(stop_output):
                    stop_code = cmd_sprint_stop(Path(tmpdir))
                restart_output = io.StringIO()
                with redirect_stdout(restart_output):
                    restart_code = cmd_sprint_restart(Path(tmpdir))

            self.assertEqual(stop_code, 0)
            self.assertEqual(restart_code, 0)
            self.assertEqual(service_mock.call_args_list[0].args, (Path(tmpdir), "orchestrator"))
            self.assertEqual(service_mock.call_args_list[0].kwargs, {"enable_discord_client": False})
            self.assertEqual(service_mock.call_args_list[1].args, (Path(tmpdir), "orchestrator"))
            self.assertEqual(service_mock.call_args_list[1].kwargs, {"enable_discord_client": False})
            stop_service.stop_sprint_lifecycle.assert_awaited_once_with(resume_mode="await")
            restart_service.restart_sprint_lifecycle.assert_awaited_once_with(resume_mode="await")
            self.assertIn("stop requested", stop_output.getvalue())
            self.assertIn("restart requested", restart_output.getvalue())

    def test_main_sprint_status_command_renders_sprint_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            write_json(
                paths.sprint_scheduler_file,
                {
                    "active_sprint_id": "260324-Sprint-09:00",
                    "next_slot_at": "2026-03-24T03:00:00+00:00",
                },
            )
            write_json(
                paths.sprint_file("260324-Sprint-09:00"),
                {
                    "sprint_id": "260324-Sprint-09:00",
                    "sprint_name": "status-cli",
                    "phase": "ongoing",
                    "milestone_title": "CLI status",
                    "status": "running",
                    "trigger": "manual_start",
                    "started_at": "2026-03-24T00:00:00+00:00",
                    "ended_at": "",
                    "closeout_status": "",
                },
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["sprint", "status", "--workspace-root", tmpdir])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("## Sprint Summary", rendered)
            self.assertIn("sprint_id: 260324-Sprint-09:00", rendered)
            self.assertIn("milestone_title: CLI status", rendered)

    def test_main_status_accepts_internal_sourcer_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["status", "--workspace-root", tmpdir, "--agent", "sourcer"])

            self.assertEqual(exit_code, 0)
            self.assertIn("sourcer: status=stopped", output.getvalue())
            self.assertIn("model=N/A reasoning=N/A", output.getvalue())
            self.assertIn("listener=n/a", output.getvalue())

    def test_main_status_accepts_internal_version_controller_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["status", "--workspace-root", tmpdir, "--agent", "version_controller"])

            self.assertEqual(exit_code, 0)
            self.assertIn("version_controller: status=stopped", output.getvalue())
            self.assertIn("model=N/A reasoning=N/A", output.getvalue())
            self.assertIn("listener=n/a", output.getvalue())

    def test_main_status_surfaces_internal_agent_listener_health(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            write_json(
                paths.agent_state_file("sourcer"),
                {
                    "role": "sourcer",
                    "listener_status": "reconnecting",
                    "listener_error_category": "client_disconnected",
                },
            )
            output = io.StringIO()
            with patch("teams_runtime.cli.role_service_status", return_value=(True, 42424)):
                with redirect_stdout(output):
                    exit_code = main(["status", "--workspace-root", tmpdir, "--agent", "sourcer"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("sourcer: status=running pid=42424", rendered)
            self.assertIn("listener=reconnecting", rendered)
            self.assertIn("listener_error=client_disconnected", rendered)

    def test_internal_sourcer_service_connects_discord_presence_with_internal_bot_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            self._write_runtime_ready_discord_config(tmpdir)
            service = InternalAgentService(Path(tmpdir), "sourcer")
            captured: dict[str, object] = {}

            class FakeDiscordClient:
                def __init__(self, **kwargs):
                    captured.update(kwargs)

                async def listen(self, _on_message, on_ready=None):
                    if on_ready is not None:
                        await on_ready()

                def current_identity(self):
                    return {"id": "123456789012345685", "name": "CS_ADMIN"}

            with patch("teams_runtime.cli.DiscordClient", FakeDiscordClient):
                asyncio.run(service._run_discord_presence_once())

            self.assertEqual(captured["token_env"], "AGENT_DISCORD_TOKEN_CS_ADMIN")
            self.assertEqual(captured["expected_bot_id"], "123456789012345685")
            self.assertEqual(captured["client_name"], "sourcer")
            self.assertEqual(captured["transcript_log_file"], service.paths.agent_discord_log("sourcer"))
            state = read_json(service.paths.agent_state_file("sourcer"))
            self.assertEqual(state["listener_status"], "connected")
            self.assertEqual(state["listener_connected_bot_name"], "CS_ADMIN")
            self.assertEqual(state["listener_connected_bot_id"], "123456789012345685")
            self.assertEqual(state["listener_expected_bot_id"], "123456789012345685")
            self.assertEqual(state["listener_discord_config_path"], str((Path(tmpdir).resolve() / "discord_agents_config.yaml")))
            self.assertEqual(state["listener_resolved_workspace_root"], str(Path(tmpdir).resolve()))

    def test_internal_sourcer_service_records_listener_failure_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            self._write_runtime_ready_discord_config(tmpdir)
            service = InternalAgentService(Path(tmpdir), "sourcer")

            class FailingDiscordClient:
                def __init__(self, **_kwargs):
                    return None

                async def listen(self, _on_message, on_ready=None):
                    raise DiscordListenError("Discord client disconnected during gateway resume")

            async def cancel_sleep(_seconds):
                raise asyncio.CancelledError()

            with patch("teams_runtime.cli.DiscordClient", FailingDiscordClient):
                with patch("teams_runtime.cli.asyncio.sleep", side_effect=cancel_sleep):
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(service._listen_forever())

            state = read_json(service.paths.agent_state_file("sourcer"))
            self.assertEqual(state["listener_status"], "reconnecting")
            self.assertEqual(state["listener_error_category"], "client_disconnected")
            self.assertIn("disconnected", state["listener_error"].lower())

    def test_main_status_omits_reload_fields_for_running_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)

            output = io.StringIO()
            with patch("teams_runtime.cli.role_service_status", return_value=(True, 42424)):
                with redirect_stdout(output):
                    exit_code = main(["status", "--workspace-root", tmpdir, "--agent", "planner"])

            self.assertEqual(exit_code, 0)
            self.assertIn("planner: status=running pid=42424", output.getvalue())
            self.assertNotIn("reload=", output.getvalue())

    def test_main_status_prints_single_sprint_id_for_agent_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            write_json(
                paths.session_state_file("planner"),
                {
                    "sprint_id": "260324-Sprint-09:00",
                    "session_id": "session-1",
                },
            )

            output = io.StringIO()
            with patch("teams_runtime.cli.role_service_status", return_value=(True, 42424)):
                with redirect_stdout(output):
                    exit_code = main(["status", "--workspace-root", tmpdir, "--agent", "planner"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("planner: status=running pid=42424", rendered)
            self.assertIn("sprint_id=260324-Sprint-09:00", rendered)
            self.assertIn("model=gpt-5.4 reasoning=xhigh", rendered)
            self.assertNotIn("sprint_series_id", rendered)

    def test_main_list_prints_active_sprint_id_from_scheduler(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            write_json(
                paths.sprint_scheduler_file,
                {
                    "active_sprint_id": "260330-Sprint-20:26",
                    "next_slot_at": "2026-03-30T23:26:00+09:00",
                },
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["list", "--workspace-root", tmpdir])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("orchestrator: stopped pid=N/A model=gpt-5.4 reasoning=medium", rendered)
            self.assertIn("developer: stopped pid=N/A model=gpt-5.3-codex-spark reasoning=xhigh", rendered)
            self.assertIn("active_sprint_id=260330-Sprint-20:26", rendered)
            self.assertNotIn("sprint_series_id", rendered)

    def test_main_config_role_set_updates_runtime_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "config",
                        "role",
                        "set",
                        "--workspace-root",
                        tmpdir,
                        "--agent",
                        "qa",
                        "--model",
                        "gemini-2.5-pro",
                    ]
                )

            self.assertEqual(exit_code, 0)
            runtime_config = load_team_runtime_config(tmpdir)
            self.assertEqual(runtime_config.role_defaults["qa"].model, "gemini-2.5-pro")
            self.assertEqual(runtime_config.role_defaults["qa"].reasoning, "medium")
            rendered = output.getvalue()
            self.assertIn("role=qa model=gemini-2.5-pro reasoning=None", rendered)

    def test_main_config_role_set_requires_model_or_reasoning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            with self.assertRaisesRegex(ValueError, "At least one of model or reasoning"):
                main(
                    [
                        "config",
                        "role",
                        "set",
                        "--workspace-root",
                        tmpdir,
                        "--agent",
                        "qa",
                    ]
                )

    def test_run_foreground_role_service_does_not_persist_reload_snapshot_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)

            async def noop() -> None:
                return None

            asyncio.run(run_foreground_role_service(paths, "sourcer", noop))

            state = read_json(paths.agent_state_file("sourcer"))
            self.assertEqual(state["status"], "stopped")
            self.assertNotIn("startup_runtime_snapshot", state)
            self.assertNotIn("reload_required", state)

    def test_role_service_status_detects_orphan_listener_from_process_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            orphan_pid = 42424
            command = (
                f"/opt/anaconda3/envs/mdva/bin/python -u -m teams_runtime.cli run "
                f"--workspace-root {paths.workspace_root} --agent planner"
            )
            with patch(
                "teams_runtime.discord.lifecycle.subprocess.run",
                return_value=SimpleNamespace(stdout=f"{orphan_pid} {command}\n"),
            ):
                with patch("teams_runtime.discord.lifecycle.is_process_running", return_value=True):
                    running, pid = role_service_status(paths, "planner")
            self.assertTrue(running)
            self.assertEqual(pid, orphan_pid)

    def test_role_service_status_handles_process_table_permission_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            with patch(
                "teams_runtime.discord.lifecycle.subprocess.run",
                side_effect=PermissionError("ps denied"),
            ):
                running, pid = role_service_status(paths, "planner")
            self.assertFalse(running)
            self.assertIsNone(pid)

    def test_start_background_role_service_rejects_orphan_listener_duplicates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            orphan_pid = 42424
            command = (
                f"/opt/anaconda3/envs/mdva/bin/python -u -m teams_runtime.cli run "
                f"--workspace-root {paths.workspace_root} --agent planner"
            )
            with patch(
                "teams_runtime.discord.lifecycle.subprocess.run",
                return_value=SimpleNamespace(stdout=f"{orphan_pid} {command}\n"),
            ):
                with patch("teams_runtime.discord.lifecycle.is_process_running", return_value=True):
                    with patch("teams_runtime.discord.lifecycle.subprocess.Popen") as popen_mock:
                        with self.assertRaisesRegex(RuntimeError, "already running with PID 42424"):
                            start_background_role_service(paths, "planner")
                        popen_mock.assert_not_called()

    def test_start_background_role_service_rotates_existing_runtime_log_and_writes_session_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime_log = paths.agent_runtime_log("planner")
            runtime_log.parent.mkdir(parents=True, exist_ok=True)
            runtime_log.write_text("old failure line\n", encoding="utf-8")

            fake_process = SimpleNamespace(pid=42424, poll=lambda: None)
            with patch(
                "teams_runtime.discord.lifecycle.subprocess.run",
                return_value=SimpleNamespace(stdout=""),
            ):
                    with patch("teams_runtime.discord.lifecycle.subprocess.Popen", return_value=fake_process):
                        with patch("teams_runtime.discord.lifecycle.time.sleep", return_value=None):
                            pid = start_background_role_service(paths, "planner")

            self.assertEqual(pid, 42424)
            archived_logs = sorted(paths.agent_log_archive_dir.glob("planner-*.log"))
            self.assertEqual(len(archived_logs), 1)
            self.assertEqual(archived_logs[0].read_text(encoding="utf-8"), "old failure line\n")
            current_log = runtime_log.read_text(encoding="utf-8")
            self.assertIn("[teams_runtime] service_start role=planner", current_log)
            self.assertNotIn("old failure line", current_log)

    def test_role_service_status_records_stale_pid_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            paths.agent_pid_file("planner").parent.mkdir(parents=True, exist_ok=True)
            paths.agent_pid_file("planner").write_text("42424\n", encoding="utf-8")

            with patch(
                "teams_runtime.discord.lifecycle.subprocess.run",
                return_value=SimpleNamespace(stdout=""),
            ):
                with patch("teams_runtime.discord.lifecycle.is_process_running", return_value=False):
                    running, pid = role_service_status(paths, "planner")

            self.assertFalse(running)
            self.assertIsNone(pid)
            state = read_json(paths.agent_state_file("planner"))
            self.assertEqual(state["process_status"], "stale_pid")
            self.assertIn("stale pid", state["recovery_action"])

    def test_start_background_role_service_records_failed_state_on_immediate_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime_log = paths.agent_runtime_log("planner")
            runtime_log.parent.mkdir(parents=True, exist_ok=True)
            runtime_log.write_text("login failure\n", encoding="utf-8")

            fake_process = SimpleNamespace(pid=42424, poll=lambda: 1)
            with patch(
                "teams_runtime.discord.lifecycle.subprocess.run",
                return_value=SimpleNamespace(stdout=""),
            ):
                with patch("teams_runtime.discord.lifecycle.subprocess.Popen", return_value=fake_process):
                    with patch("teams_runtime.discord.lifecycle.time.sleep", return_value=None):
                        with self.assertRaisesRegex(RuntimeError, "exited immediately after launch"):
                            start_background_role_service(paths, "planner")

            state = read_json(paths.agent_state_file("planner"))
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["process_status"], "exited_immediately")
            self.assertIn("service_start role=planner", state["last_error"])

    def test_stop_background_role_service_stops_orphan_listener_without_pid_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            orphan_pid = 42424
            command = (
                f"/opt/anaconda3/envs/mdva/bin/python -u -m teams_runtime.cli run "
                f"--workspace-root {paths.workspace_root} --agent planner"
            )
            with patch(
                "teams_runtime.discord.lifecycle.subprocess.run",
                return_value=SimpleNamespace(stdout=f"{orphan_pid} {command}\n"),
            ):
                with (
                    patch(
                        "teams_runtime.discord.lifecycle.is_process_running",
                        side_effect=lambda pid: False if pid == orphan_pid and getattr(self, "_killed", False) else pid == orphan_pid,
                    ),
                    patch("teams_runtime.discord.lifecycle.os.kill", side_effect=lambda pid, sig: setattr(self, "_killed", True)),
                ):
                    self._killed = False
                    stopped, message = stop_background_role_service(paths, "planner", timeout_seconds=0.5)
            self.assertTrue(stopped)
            self.assertIn("Stopped planner service with PID 42424", message)

    def test_is_process_running_treats_zombie_process_as_stopped(self):
        with patch("teams_runtime.discord.lifecycle.os.kill", return_value=None):
            with patch(
                "teams_runtime.discord.lifecycle.subprocess.run",
                return_value=SimpleNamespace(stdout="Z+\n"),
            ):
                self.assertFalse(is_process_running(42424))

    def test_stop_background_role_service_force_kills_stubborn_process_after_sigterm_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            orphan_pid = 42424
            command = (
                f"/opt/anaconda3/envs/mdva/bin/python -u -m teams_runtime run "
                f"--workspace-root {paths.workspace_root} --agent planner"
            )
            with patch(
                "teams_runtime.discord.lifecycle.subprocess.run",
                return_value=SimpleNamespace(stdout=f"{orphan_pid} {command}\n"),
            ):
                signals_sent: list[signal.Signals] = []
                running_state = {"alive": True}

                def fake_is_process_running(pid: int) -> bool:
                    return pid == orphan_pid and running_state["alive"]

                def fake_killpg(pid: int, sig: signal.Signals) -> None:
                    signals_sent.append(sig)
                    if sig == signal.SIGKILL:
                        running_state["alive"] = False

                with (
                    patch("teams_runtime.discord.lifecycle.is_process_running", side_effect=fake_is_process_running),
                    patch("teams_runtime.discord.lifecycle.os.getpgid", return_value=orphan_pid),
                    patch("teams_runtime.discord.lifecycle.os.killpg", side_effect=fake_killpg),
                ):
                    stopped, message = stop_background_role_service(paths, "planner", timeout_seconds=0.0)

            self.assertTrue(stopped)
            self.assertEqual(signals_sent, [signal.SIGTERM, signal.SIGKILL])
            self.assertIn("Force-stopped planner service with PID 42424", message)
