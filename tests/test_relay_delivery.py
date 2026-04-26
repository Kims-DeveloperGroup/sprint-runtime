from __future__ import annotations

import unittest

from teams_runtime.core.relay_delivery import (
    apply_relay_delivery_status,
    build_relay_delivery_failure_payload,
    relay_delivery_failure_summary,
)


class TeamsRuntimeRelayDeliveryTests(unittest.TestCase):
    def test_apply_relay_delivery_status_updates_request_fields(self):
        request_record = {}

        apply_relay_delivery_status(
            request_record,
            status="failed",
            target_description="relay:111111111111111111",
            attempts=3,
            error="TimeoutError: timeout",
            updated_at="2026-04-19T20:00:00+09:00",
        )

        self.assertEqual(request_record["relay_send_status"], "failed")
        self.assertEqual(request_record["relay_send_target"], "relay:111111111111111111")
        self.assertEqual(request_record["relay_send_attempts"], 3)
        self.assertEqual(request_record["relay_send_error"], "TimeoutError: timeout")
        self.assertEqual(request_record["relay_send_updated_at"], "2026-04-19T20:00:00+09:00")

    def test_build_relay_delivery_failure_payload_truncates_scope(self):
        payload = build_relay_delivery_failure_payload(
            target_description="relay:111111111111111111",
            attempts=3,
            error="TimeoutError: timeout",
            envelope_target="planner",
            intent="route",
            scope="word " * 60,
        )

        self.assertEqual(payload["target"], "relay:111111111111111111")
        self.assertEqual(payload["attempts"], 3)
        self.assertEqual(payload["envelope_target"], "planner")
        self.assertEqual(payload["intent"], "route")
        self.assertLessEqual(len(payload["scope"]), 120)

    def test_relay_delivery_failure_summary_formats_target(self):
        self.assertEqual(
            relay_delivery_failure_summary("relay:111111111111111111"),
            "relay 채널 전송이 실패했습니다. target=relay:111111111111111111",
        )


if __name__ == "__main__":
    unittest.main()
