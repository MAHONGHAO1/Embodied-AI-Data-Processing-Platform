"""Lightweight workflow exception shared by the reset-domain services."""

from __future__ import annotations


class WorkflowConflict(ValueError):
    """The requested action conflicts with immutable workflow facts."""
