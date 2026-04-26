from __future__ import annotations

"""Compatibility facade for ingress parsing helpers."""

from teams_runtime.workflows.orchestration import ingress as _ingress

__all__ = list(_ingress._PARSING_EXPORTS)

globals().update({name: getattr(_ingress, name) for name in __all__})
