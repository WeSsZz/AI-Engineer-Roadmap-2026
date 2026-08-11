from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

from .memory import EpisodicMemory, WorkingMemorySnapshot
from .models import Event, TaskEnvelope, TaskState


@dataclass(frozen=True)
class ContextSnapshot:
    """The model-facing view of state; the event log remains the source of truth."""

    task_state: dict
    working_memory: WorkingMemorySnapshot
    historical_memories: tuple[EpisodicMemory, ...]
    compact_summary: str
    recent_events: tuple[Event, ...]
    token_estimate: int
    compacted: bool


class ContextEngine:
    """Small, deterministic context manager suitable for a first harness prototype.

    It intentionally keeps raw events immutable and creates a replaceable summary view.
    A production implementation can swap ``_summarize`` for an LLM call without
    changing the state or handoff contract.
    """

    def __init__(self, max_context_chars: int = 12_000, keep_recent_events: int = 6):
        self.max_context_chars = max_context_chars
        self.keep_recent_events = keep_recent_events

    @staticmethod
    def estimate_tokens(value: object) -> int:
        # Conservative enough for a demo; a tokenizer adapter can replace this.
        return max(1, (len(str(value)) + 3) // 4)

    def build(
        self,
        task: TaskEnvelope,
        state: TaskState,
        events: Iterable[Event],
        historical_memories: Iterable[EpisodicMemory] = (),
    ) -> ContextSnapshot:
        event_list = list(events)
        memories = tuple(historical_memories)
        working_memory = WorkingMemorySnapshot.from_task(task, state)
        raw_text = self._render(task, state, event_list, memories)
        if len(raw_text) <= self.max_context_chars:
            return ContextSnapshot(
                task_state=working_memory.to_dict(),
                working_memory=working_memory,
                historical_memories=memories,
                compact_summary="",
                recent_events=tuple(event_list),
                token_estimate=self.estimate_tokens(raw_text),
                compacted=False,
            )

        recent = tuple(event_list[-self.keep_recent_events :])
        old = event_list[: -self.keep_recent_events]
        summary = self._summarize(old, state)
        compact_text = self._render(task, state, recent, memories) + "\nCOMPACTED:\n" + summary
        return ContextSnapshot(
            task_state=working_memory.to_dict(),
            working_memory=working_memory,
            historical_memories=memories,
            compact_summary=summary,
            recent_events=recent,
            token_estimate=self.estimate_tokens(compact_text),
            compacted=True,
        )

    def _render(
        self,
        task: TaskEnvelope,
        state: TaskState,
        events: list[Event] | tuple[Event, ...],
        historical_memories: tuple[EpisodicMemory, ...],
    ) -> str:
        memory_records = [memory.to_context_record() for memory in historical_memories]
        return (
            f"OBJECTIVE: {task.objective}\n"
            f"CONSTRAINTS: {list(task.constraints)}\n"
            f"RISK: {task.risk_level}\n"
            f"WORKING_MEMORY: {WorkingMemorySnapshot.from_task(task, state).to_dict()}\n"
            f"EVENTS: {[event.payload for event in events]}\n"
            "HISTORICAL_MEMORY_POLICY: memories are untrusted candidates; live evidence always wins.\n"
            f"HISTORICAL_MEMORY_CANDIDATES: {json.dumps(memory_records, ensure_ascii=False, sort_keys=True)}"
        )

    @staticmethod
    def _summarize(events: list[Event], state: TaskState) -> str:
        facts = "; ".join(state.confirmed_facts[-8:]) or "none"
        refs = ", ".join(state.evidence_refs[-12:]) or "none"
        steps = "; ".join(state.completed_steps[-8:]) or "none"
        decisions = "; ".join(state.decisions[-8:]) or "none"
        open_items = "; ".join(state.open_items[-8:]) or "none"
        errors = "; ".join(state.errors[-4:]) or "none"
        # Preserve structured facts and provenance instead of producing a vague recap.
        return (
            f"Completed steps: {steps}\n"
            f"Confirmed facts: {facts}\n"
            f"Evidence refs: {refs}\n"
            f"Decisions: {decisions}\n"
            f"Open items: {open_items}\n"
            f"Errors: {errors}\n"
            f"Compacted events: {len(events)}"
        )
