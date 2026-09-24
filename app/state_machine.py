"""Product state machine.

States:
  RECEIVED -> RESOLVING -> ANALYZED -> QUEUED -> WAITING_FOR_OPPORTUNITY
           -> GENERATING -> REVIEWING -> READY_TO_PUBLISH -> PUBLISHING
           -> PUBLISHED -> ANALYTICS_PENDING -> COMPLETED
  Any state -> FAILED | SKIPPED (terminal)

Instant mode skips QUEUED/WAITING_FOR_OPPORTUNITY.
Transitions are validated; invalid transitions raise StateError.
"""
from __future__ import annotations

from dataclasses import dataclass

TERMINAL_STATES = {"COMPLETED", "FAILED", "SKIPPED"}


class StateError(Exception):
    pass


@dataclass(frozen=True)
class Transition:
    src: str
    dst: str


_ALLOWED: set[Transition] = {
    Transition("RECEIVED", "RESOLVING"),
    Transition("RECEIVED", "FAILED"),
    Transition("RESOLVING", "ANALYZED"),
    Transition("RESOLVING", "FAILED"),
    Transition("RESOLVING", "SKIPPED"),
    Transition("ANALYZED", "QUEUED"),
    Transition("ANALYZED", "GENERATING"),  # instant mode
    Transition("ANALYZED", "FAILED"),
    Transition("QUEUED", "WAITING_FOR_OPPORTUNITY"),
    Transition("QUEUED", "GENERATING"),  # instant mode / evergreen fallback
    Transition("QUEUED", "SKIPPED"),
    Transition("WAITING_FOR_OPPORTUNITY", "GENERATING"),
    Transition("WAITING_FOR_OPPORTUNITY", "SKIPPED"),
    Transition("WAITING_FOR_OPPORTUNITY", "FAILED"),
    Transition("GENERATING", "REVIEWING"),
    Transition("GENERATING", "FAILED"),
    Transition("REVIEWING", "READY_TO_PUBLISH"),
    Transition("REVIEWING", "SKIPPED"),  # all candidates rejected
    Transition("REVIEWING", "FAILED"),
    Transition("READY_TO_PUBLISH", "PUBLISHING"),
    Transition("READY_TO_PUBLISH", "SKIPPED"),  # rate-limited back / similarity
    Transition("READY_TO_PUBLISH", "GENERATING"),  # recovery: regenerate
    Transition("PUBLISHING", "PUBLISHED"),
    Transition("PUBLISHING", "FAILED"),
    Transition("PUBLISHING", "READY_TO_PUBLISH"),  # retryable publish failure
    Transition("PUBLISHED", "ANALYTICS_PENDING"),
    Transition("PUBLISHED", "COMPLETED"),  # dry run / no analytics possible
    Transition("ANALYTICS_PENDING", "COMPLETED"),
    Transition("ANALYTICS_PENDING", "FAILED"),
    # recovery helpers (idempotent restarts)
    Transition("FAILED", "RESOLVING"),
    Transition("FAILED", "ANALYZED"),
    Transition("FAILED", "GENERATING"),
    Transition("FAILED", "READY_TO_PUBLISH"),
}


def can_transition(src: str, dst: str) -> bool:
    return Transition(src, dst) in _ALLOWED


def transition(src: str, dst: str) -> None:
    if not can_transition(src, dst):
        raise StateError(f"Illegal state transition: {src} -> {dst}")


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def next_states(src: str) -> list[str]:
    return sorted({t.dst for t in _ALLOWED if t.src == src})
