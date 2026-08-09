from __future__ import annotations

from .models import TaskState, TaskStatus


class InvalidStateTransition(RuntimeError):
    pass


_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.RUNNING_SPECIALISTS, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING_SPECIALISTS: frozenset({TaskStatus.VERIFYING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.VERIFYING: frozenset({TaskStatus.SYNTHESIZING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.SYNTHESIZING: frozenset(
        {TaskStatus.WAITING_APPROVAL, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.EXECUTING_ACTION, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.EXECUTING_ACTION: frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})


def transition(state: TaskState, target: TaskStatus) -> None:
    allowed = _ALLOWED_TRANSITIONS[state.status]
    if target not in allowed:
        raise InvalidStateTransition(f"Cannot transition task {state.task_id} from {state.status.value} to {target.value}")
    state.status = target
