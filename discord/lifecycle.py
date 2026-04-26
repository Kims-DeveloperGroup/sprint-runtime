"""Compatibility alias for the Discord lifecycle adapter module."""

from __future__ import annotations

import sys

from teams_runtime.adapters.discord import lifecycle as _lifecycle

sys.modules[__name__] = _lifecycle
