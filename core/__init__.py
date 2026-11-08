"""Tasuki Hermes plugin core: pairing, client, delegation worker, data tools.

Loaded by path from both entry points (the plugin's register() and the dashboard API file) under the single
module name ``_tasuki_hermes_core``, so one process holds exactly one worker and one set of state.
"""
from __future__ import annotations

VERSION = "0.4.1"
PROTOCOL_VERSION = 1
# Onboarding features the dashboard API offers (status.features): the app checks these instead of guessing from the version.
FEATURES = [
    "claim",
    "enroll_device",
    "pair_legacy",
]
PLUGIN_NAME = "tasuki"
CAPABILITIES = [
    "delegation",
    "tools.mail.read",
    "tools.mail.draft",
    "tools.calendar.read",
    "tools.food.read",
    "tasks.mirror",
    "tasks.inbox",
]
