"""Public plugin-facing types.

This package is what an **plugin** imports to fill a slot. Nothing in here may
import the frozen :mod:`xu_brain.core` spine backwards; it is the vocabulary the
Core hands to plugins through :class:`~xu_brain.api.context.PluginContext`.
"""
from __future__ import annotations

from .context import PluginContext
from .events import EVENT_POINTS, EVENTS, TRANSFORM_POINTS
from .manifest import Manifest, load_manifest, validate_manifest

__all__ = [
    "EVENT_POINTS",
    "EVENTS",
    "Manifest",
    "PluginContext",
    "TRANSFORM_POINTS",
    "load_manifest",
    "validate_manifest",
]
