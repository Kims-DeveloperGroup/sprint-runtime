"""Compatibility alias for orchestration ingress requester-route helpers."""

from __future__ import annotations

import sys

from teams_runtime.workflows.orchestration import ingress as _ingress

sys.modules[__name__] = _ingress
