from __future__ import annotations

import unittest

from teams_runtime.core.relay_summary import (
    build_internal_relay_summary_message,
    relay_summary_text_fragments,
)
from teams_runtime.shared.models import MessageEnvelope


class TeamsRuntimeRelaySummaryTests(unittest.TestCase):
    def test_build_internal_relay_summary_message_renders_structured_sections(self):
        envelope = MessageEnvelope(
            request_id="20260419-relay-summary-1",
            sender="planner",
            target="developer",
            intent="delegate",
            urgency="normal",
            scope="scope",
            params={"_teams_kind": "delegate"},
        )

        content = build_internal_relay_summary_message(
            envelope,
            marker="내부 relay 요약:",
            summary_lines=[
                "- Why now: planner 문서가 끝났습니다.",
                "- What: 실행 backlog/todo 2건을 정리했습니다.",
                "- Check now:",
                "  - backlog/todo: auth gate",
                "  - backlog/todo: audit trail",
                "- Refs:",
                "  - shared_workspace/backlog.md",
            ],
        )

        self.assertTrue(content.startswith("내부 relay 요약: planner -> developer (delegate)"))
        self.assertIn("[전달 정보]", content)
        self.assertIn("[이관 이유]", content)
        self.assertIn("[핵심 전달]", content)
        self.assertIn("[지금 볼 것]", content)
        self.assertIn("[참고 파일]", content)

    def test_relay_summary_text_fragments_collapses_multiline_whitespace(self):
        fragments = relay_summary_text_fragments(
            "첫 번째 줄입니다.\n\n두 번째   줄입니다.\n세 번째 줄입니다.",
            width=80,
            max_lines=2,
        )

        self.assertEqual(
            fragments,
            [
                "첫 번째 줄입니다.",
                "두 번째 줄입니다.",
                "... 외 1줄",
            ],
        )


if __name__ == "__main__":
    unittest.main()
