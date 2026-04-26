"""Compatibility alias for the target TeamService composition module."""

from __future__ import annotations

import sys

from teams_runtime.workflows.orchestration import team_service as _team_service

sys.modules[__name__] = _team_service
