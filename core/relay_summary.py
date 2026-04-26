"""Compatibility alias for relay summary helpers."""

from __future__ import annotations

import sys

from teams_runtime.workflows.orchestration import relay as _relay

sys.modules[__name__] = _relay
