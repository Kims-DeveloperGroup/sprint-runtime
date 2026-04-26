"""Compatibility alias for the Discord client adapter module."""

from __future__ import annotations

import sys

from teams_runtime.adapters.discord import client as _client

sys.modules[__name__] = _client
