"""Compatibility alias for orchestration notification helpers."""

from __future__ import annotations

import sys

from teams_runtime.workflows.orchestration import notifications as _notifications

sys.modules[__name__] = _notifications
