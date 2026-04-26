from __future__ import annotations

"""Compatibility facade for workflow agent utilization policy helpers.

Remove this shim after internal imports and tests use
``teams_runtime.workflows.roles`` directly.
"""

from teams_runtime.workflows import roles as _roles

__all__ = list(_roles._AGENT_CAPABILITY_EXPORTS)

globals().update({name: getattr(_roles, name) for name in __all__})
