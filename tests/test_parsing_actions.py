from __future__ import annotations

import json
import os
import signal
import tempfile
import unittest

from teams_runtime.core.actions import ActionExecutor
from teams_runtime.core.parsing import (
    detect_message_shape,
    detect_target_role_from_mentions,
    envelope_to_text,
    parse_message_content,
    parse_user_message_content,
)
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.models import ActionConfig, MessageEnvelope, RoleRuntimeConfig, TeamRuntimeConfig
from teams_runtime.runtime.internal.intent_parser import (
    IntentParserRuntime,
    infer_status_inquiry_payload,
    normalize_intent_payload,
)


class TeamsRuntimeParsingAndActionsTests(unittest.TestCase):
    def test_parse_message_content_handles_mentions_and_freeform_commands(self):
        bot_ids = {
            "orchestrator": "100",
            "planner": "101",
            "designer": "102",
            "architect": "103",
            "developer": "104",
            "qa": "105",
        }
        envelope = parse_message_content(
            "<@104>\nintent: implement\nscope: Build the thing\nparams: {\"ticket\": \"ABC-1\"}",
            bot_ids_by_role=bot_ids,
            default_target="orchestrator",
        )

        self.assertEqual(envelope.target, "developer")
        self.assertEqual(envelope.intent, "implement")
        self.assertEqual(envelope.params["ticket"], "ABC-1")
        self.assertEqual(envelope.scope, "Build the thing")
        self.assertEqual(detect_target_role_from_mentions("<@!103>", bot_ids), "architect")

        freeform = parse_message_content(
            "<@101> intraday trade 개선 방안 기획해",
            bot_ids_by_role=bot_ids,
            default_target="orchestrator",
        )
        self.assertEqual(freeform.target, "planner")
        self.assertEqual(freeform.intent, "route")
        self.assertEqual(freeform.scope, "intraday trade 개선 방안 기획해")
        self.assertEqual(freeform.body, "intraday trade 개선 방안 기획해")

        qa_request = parse_message_content(
            "<@105>\nintent: qa\nscope: 회귀 테스트 누락 여부를 검토해줘",
            bot_ids_by_role=bot_ids,
            default_target="orchestrator",
        )
        self.assertEqual(qa_request.target, "qa")
        self.assertEqual(qa_request.intent, "qa")
        self.assertEqual(qa_request.scope, "회귀 테스트 누락 여부를 검토해줘")

        approve = parse_message_content("approve request_id: 20260322-deadbeef")
        self.assertEqual(approve.intent, "approve")
        self.assertEqual(approve.request_id, "20260322-deadbeef")

        sprint_status = parse_message_content("status sprint")
        self.assertEqual(sprint_status.intent, "status")
        self.assertEqual(sprint_status.scope, "sprint")

        natural_language = parse_message_content(
            "<@100> 현재 스프린트 공유해줘",
            bot_ids_by_role=bot_ids,
            default_target="orchestrator",
        )
        self.assertEqual(natural_language.target, "orchestrator")
        self.assertEqual(natural_language.intent, "route")
        self.assertEqual(natural_language.scope, "현재 스프린트 공유해줘")
        self.assertEqual(
            detect_message_shape("<@100> 현재 스프린트 공유해줘", bot_ids_by_role=bot_ids),
            "freeform",
        )

        sprint_start = parse_message_content(
            "start sprint\nmilestone: sprint workflow initial phase 개선",
            bot_ids_by_role=bot_ids,
            default_target="orchestrator",
        )
        self.assertEqual(sprint_start.intent, "route")
        self.assertEqual(sprint_start.scope, "start sprint\nmilestone: sprint workflow initial phase 개선")
        self.assertIn("milestone: sprint workflow initial phase 개선", sprint_start.body)
        self.assertEqual(
            detect_message_shape(
                "start sprint\nmilestone: sprint workflow initial phase 개선",
                bot_ids_by_role=bot_ids,
            ),
            "freeform",
        )

    def test_parse_user_message_content_synthesizes_attachment_only_body_and_scope(self):
        envelope = parse_user_message_content(
            "",
            artifacts=["./shared_workspace/sprints/2026-Sprint-01/attachments/att-1_note.txt"],
            default_target="orchestrator",
        )

        self.assertEqual(envelope.intent, "route")
        self.assertEqual(envelope.scope, "첨부 파일 1건이 포함된 사용자 요청")
        self.assertEqual(envelope.body, "첨부 파일 1건이 포함된 사용자 요청")
        self.assertEqual(
            envelope.artifacts,
            ["./shared_workspace/sprints/2026-Sprint-01/attachments/att-1_note.txt"],
        )

    def test_envelope_to_text_omits_from_and_to_but_parser_keeps_backward_compatibility(self):
        envelope = parse_message_content(
            "request_id: 20260323-abcd1234\nfrom: orchestrator\nto: developer\nintent: implement\nscope: Build the thing",
            default_sender="user",
            default_target="orchestrator",
        )

        self.assertEqual(envelope.sender, "orchestrator")
        self.assertEqual(envelope.target, "developer")

        rendered = envelope_to_text(envelope)
        self.assertIn("request_id: 20260323-abcd1234", rendered)
        self.assertIn("intent: implement", rendered)
        self.assertNotIn("from:", rendered)
        self.assertNotIn("to:", rendered)

    def test_infer_status_inquiry_payload_handles_english_sprint_and_backlog_questions(self):
        sprint_payload = infer_status_inquiry_payload("What sprint is ongoing?")
        self.assertIsNotNone(sprint_payload)
        self.assertEqual(sprint_payload["intent"], "status")
        self.assertEqual(sprint_payload["scope"], "sprint")

        typo_payload = infer_status_inquiry_payload("What is the current spring workign for?")
        self.assertIsNotNone(typo_payload)
        self.assertEqual(typo_payload["intent"], "status")
        self.assertEqual(typo_payload["scope"], "sprint")

        backlog_payload = infer_status_inquiry_payload("What are todos in backlog?")
        self.assertIsNotNone(backlog_payload)
        self.assertEqual(backlog_payload["intent"], "status")
        self.assertEqual(backlog_payload["scope"], "backlog")

    def test_infer_status_inquiry_payload_ignores_explicit_sprint_control_commands(self):
        self.assertIsNone(infer_status_inquiry_payload("start sprint"))
        self.assertIsNone(infer_status_inquiry_payload("start sprint\nmilestone: sprint workflow initial phase 개선"))
        self.assertIsNone(infer_status_inquiry_payload("finalize sprint"))

    def test_normalize_intent_payload_recovers_noncanonical_status_scope(self):
        normalized = normalize_intent_payload(
            {
                "intent": "status",
                "scope": "What is the current spring workign for?",
                "body": "",
                "reason": "영문 자연어 질의",
            }
        )

        self.assertEqual(normalized["intent"], "status")
        self.assertEqual(normalized["scope"], "sprint")

    def test_normalize_intent_payload_preserves_execute_params(self):
        normalized = normalize_intent_payload(
            {
                "intent": "execute",
                "scope": "",
                "body": "",
                "params": {"action_name": "echo"},
                "reason": "등록된 액션 실행 요청",
                "confidence": "high",
            }
        )

        self.assertEqual(normalized["intent"], "execute")
        self.assertEqual(normalized["params"]["action_name"], "echo")

    def test_intent_parser_runtime_uses_codex_runner_for_status_inquiry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = IntentParserRuntime(
                paths=RuntimePaths.from_root(tmpdir),
                sprint_id="2026-Sprint-01",
                runtime_config=RoleRuntimeConfig(),
            )
            session_state = type(
                "SessionState",
                (),
                {"workspace_path": tmpdir, "session_id": "session-before"},
            )()
            runtime.session_manager.ensure_session = lambda: session_state
            runtime.session_manager.finalize_session_id = lambda state, _session_id: state
            observed: dict[str, str] = {}

            def fake_run(workspace_path, prompt, session_id):
                observed["workspace_path"] = str(workspace_path)
                observed["prompt"] = prompt
                observed["session_id"] = str(session_id or "")
                return (
                    json.dumps(
                        {
                            "intent": "status",
                            "scope": "backlog",
                            "request_id": "",
                            "body": "",
                            "params": {},
                            "reason": "자연어 backlog 상태 조회로 해석",
                            "confidence": "high",
                        }
                    ),
                    "session-after",
                )

            runtime.codex_runner.run = fake_run

            payload = runtime.classify(
                raw_text="What are todos in backlog?",
                envelope=MessageEnvelope(
                    request_id=None,
                    sender="user",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="What are todos in backlog?",
                    body="What are todos in backlog?",
                ),
                scheduler_state={},
                active_sprint={},
                backlog_counts={},
                forwarded=False,
            )

            self.assertEqual(observed["workspace_path"], tmpdir)
            self.assertIn("What are todos in backlog?", observed["prompt"])
            self.assertEqual(observed["session_id"], "session-before")
            self.assertEqual(payload["intent"], "status")
            self.assertEqual(payload["scope"], "backlog")

    def test_action_executor_runs_foreground_and_managed_actions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            runtime_config = TeamRuntimeConfig(
                sprint_id="sprint-a",
                role_defaults={"developer": RoleRuntimeConfig()},
                actions={
                    "echo": ActionConfig(
                        name="echo",
                        command=("python", "-c", "print('hello from action')"),
                        lifecycle="foreground",
                        domain="기타",
                    ),
                    "sleepy": ActionConfig(
                        name="sleepy",
                        command=("python", "-c", "import time; time.sleep(2)"),
                        lifecycle="managed",
                        domain="기타",
                    ),
                },
            )
            executor = ActionExecutor(paths, runtime_config)

            foreground = executor.execute(request_id="req-1", action_name="echo", params={})
            self.assertEqual(foreground["status"], "completed")
            self.assertNotIn("```", foreground["report"])
            self.assertNotIn("┌", foreground["report"])
            self.assertNotIn("└", foreground["report"])
            self.assertIn("[작업 보고]", foreground["report"])
            self.assertIn("🧩 요청: echo 실행", foreground["report"])
            self.assertIn("✅ 상태: 완료", foreground["report"])
            self.assertIn("🧠 판단: 등록된 액션이 실행되었습니다.", foreground["report"])
            self.assertNotIn("도메인:", foreground["report"])
            self.assertTrue(str(foreground["log_file"]).startswith(str(paths.logs_root)))

            managed = executor.execute(request_id="req-2", action_name="sleepy", params={})
            self.assertEqual(managed["status"], "running")
            self.assertTrue(str(managed["log_file"]).startswith(str(paths.logs_root)))
            status = executor.get_operation_status(managed["operation_id"])
            self.assertEqual(status["status"], "running")
            os.kill(int(managed["pid"]), signal.SIGTERM)
