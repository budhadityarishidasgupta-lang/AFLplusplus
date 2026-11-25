"""Session-level cognitive operating modes.

Modes describe how the Studio may conduct research and learning; they are not
an OS security boundary. AUTONOMOUS_AGENT is reserved for future orchestration.
"""
from enum import Enum


class OperatingMode(str, Enum):
    CONTROLLED = 'CONTROLLED'
    FREE_LEARNING = 'FREE_LEARNING'
    AUTONOMOUS_AGENT = 'AUTONOMOUS_AGENT'

    @property
    def implemented(self):
        return self is not OperatingMode.AUTONOMOUS_AGENT


def coerce_mode(value):
    """Unknown and reserved modes fail closed to controlled operation."""
    try:
        mode = value if isinstance(value, OperatingMode) else OperatingMode(value)
    except (ValueError, TypeError):
        return OperatingMode.CONTROLLED
    return mode if mode.implemented else OperatingMode.CONTROLLED


def permits_scoped_research(value):
    """Free Learning grants standing session authorization for scoped search."""
    return coerce_mode(value) is OperatingMode.FREE_LEARNING
