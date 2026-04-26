from __future__ import annotations

"""Compatibility facade for workflow state helpers."""

from teams_runtime.workflows.orchestration import engine as _engine

__all__ = list(_engine._WORKFLOW_STATE_EXPORTS)

globals().update({name: getattr(_engine, name) for name in __all__})
