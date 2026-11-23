"""Hub work-order commands (v2 02A). Every write goes through ``run_command`` (permission under lock, one
transaction, audit row; a refusal is recorded as a denied audit row).

Fixed transition graph::

    open -> assigned            (assign; a hub technician may only take an unowned task for themselves)
    assigned -> assigned        (reassign; managers only)
    assigned -> in_progress     (start; the owner, or a manager)
    in_progress -> done         (complete; the owner, or a manager)
    open | assigned | in_progress -> cancelled   (cancel with a reason; a manager, or the owner of a task they
                                                  created themselves)

Authorization is two-step: the broad permission ``hub.work`` is checked by run_command under its role-grant
lock; then the handler checks THIS task (its owner, its creator, its status) for anyone without
``hub.manage_all``. A field operator cannot start, complete or cancel someone else's task.

Work orders are not safety evidence (contract 8.1): nothing here touches holds, inspections, custody,
readiness evidence, eligibility or maintenance. The only table written is hub_workorder (plus the audit row).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils import timezone

from fleet.models import Robot
from hub.models import FINAL, TYPES, WorkOrder
from planning.models import Person
from platform_core.commands import (
    Conflict,
    Invalid,
    Outcome,
    PermissionDenied,
    holds_permission_locked,
    run_command,
)

WORK = "hub.work"
MANAGE = "hub.manage_all"
CLAIM = "hub.claim"
TYPE_WORDS = dict(TYPES)
STATUS_WORDS = {"open": "open", "assigned": "assigned", "in_progress": "in progress", "done": "done",
                "cancelled": "cancelled"}  # fmt: skip


def person_of(user: Any) -> Person | None:
    return Person.objects.select_related("user").filter(user=user).first() if user is not None else None


def _person(code: str | None) -> Person | None:
    if not code:
        return None
    p = Person.objects.select_related("user").filter(code=code).first()
    if p is None:
        raise Invalid(f"No person with code {code}")
    return p


def _lock(task_id: int, version: int | None) -> WorkOrder:
    task = WorkOrder.objects.select_for_update().filter(pk=task_id).first()
    if task is None:
        raise Invalid(f"No task {task_id}")
    if version is not None and task.version != version:
        raise Conflict(
            f"Task WO-{task.pk:04d} changed since you opened it (it is now {STATUS_WORDS[task.status]}); "
            "nothing was changed. Reload the page"
        )
    return task


def _is_manager(actor: Any) -> bool:
    return holds_permission_locked(actor, MANAGE)


def _owns(actor: Any, task: WorkOrder) -> bool:
    me = person_of(actor)
    return me is not None and task.owner_id == me.pk


def _refuse_final(task: WorkOrder, what: str) -> None:
    if task.status in FINAL:
        raise Conflict(
            f"Task WO-{task.pk:04d} is already {STATUS_WORDS[task.status]}, so it cannot be {what}"
        )


def _save(task: WorkOrder, actor: Any, **changes: Any) -> Outcome:
    for k, v in changes.items():
        setattr(task, k, v)
    task.version += 1
    task.save(update_fields=[*changes, "version", "updated_at"])
    return Outcome(
        "work_order",
        str(task.pk),
        {
            "status": task.status,
            "owner": task.owner.code if task.owner_id and task.owner else None,
            "version": task.version,
        },
    )


# ------------------------------------------------------------------ create
def create_task(
    actor: Any,
    *,
    type: str,  # noqa: A002 - the domain word
    title: str,
    robot_code: str | None = None,
    due_at: datetime | None = None,
    owner_code: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    title, note = title.strip(), note.strip()
    payload = {"type": type, "title": title, "robot": robot_code, "owner": owner_code,
               "due_at": due_at.isoformat() if due_at else None}  # fmt: skip

    def handler() -> Outcome:
        if type not in TYPE_WORDS:
            raise Invalid("Choose a task type")
        if not title or len(title) > 160:
            raise Invalid("Give the task a short title (up to 160 characters)")
        if len(note) > 300:
            raise Invalid("The note is too long (up to 300 characters)")
        robot = None
        if robot_code:
            robot = Robot.objects.filter(code=robot_code).first()
            if robot is None:
                raise Invalid(f"No robot {robot_code}")
        owner = _person(owner_code)
        if not _is_manager(actor):
            me = person_of(actor)
            if holds_permission_locked(actor, CLAIM):
                allowed = owner is None or (me is not None and owner.pk == me.pk)
            else:  # a field operator creates tasks for themselves only
                if me is None:
                    raise PermissionDenied("You need a person record to own a task; ask the ops lead")
                owner = owner or me
                allowed = owner.pk == me.pk
            if not allowed:
                raise PermissionDenied("You can only create tasks for yourself; the ops lead assigns others")
        task = WorkOrder.objects.create(
            type=type,
            title=title,
            robot=robot,
            due_at=due_at,
            owner=owner,
            status="assigned" if owner else "open",
            source_kind="manual",
            note=note,
            created_by=actor,
        )
        return Outcome(
            "work_order", str(task.pk), {"status": task.status, "owner": owner.code if owner else None}
        )

    return run_command(actor, "hub.create_task", permission=WORK, payload=payload, handler=handler)


# ------------------------------------------------------------------ transitions
def assign_task(actor: Any, *, task_id: int, owner_code: str, version: int | None = None) -> dict[str, Any]:
    payload = {"task_id": task_id, "owner": owner_code, "version": version}

    def handler() -> Outcome:
        task = _lock(task_id, version)
        _refuse_final(task, "assigned")
        owner = _person(owner_code)
        if owner is None:
            raise Invalid("Choose who does this task")
        if task.status == "in_progress":
            raise Conflict(
                f"Task WO-{task.pk:04d} is already in progress; cancel it or let the owner finish it"
            )
        if not _is_manager(actor):
            me = person_of(actor)
            if not holds_permission_locked(actor, CLAIM):
                raise PermissionDenied("Only the ops lead or engineering can assign tasks")
            if me is None or owner.pk != me.pk:
                raise PermissionDenied("You can only take a task for yourself; the ops lead assigns others")
            if task.owner_id is not None and task.owner_id != me.pk:
                raise PermissionDenied(f"Task WO-{task.pk:04d} is already assigned to someone else")
        return _save(task, actor, owner=owner, status="assigned")

    return run_command(actor, "hub.assign_task", permission=WORK, payload=payload, handler=handler)


def start_task(actor: Any, *, task_id: int, version: int | None = None) -> dict[str, Any]:
    payload = {"task_id": task_id, "version": version}

    def handler() -> Outcome:
        task = _lock(task_id, version)
        _refuse_final(task, "started")
        if task.status != "assigned":
            raise Conflict(
                f"Task WO-{task.pk:04d} is {STATUS_WORDS[task.status]}; only an assigned task can be started"
            )
        if not _is_manager(actor) and not _owns(actor, task):
            raise PermissionDenied("Only the person this task is assigned to (or the ops lead) can start it")
        return _save(task, actor, status="in_progress", started_at=timezone.now())

    return run_command(actor, "hub.start_task", permission=WORK, payload=payload, handler=handler)


def complete_task(actor: Any, *, task_id: int, version: int | None = None, note: str = "") -> dict[str, Any]:
    note = note.strip()
    payload = {"task_id": task_id, "version": version, "note": note}

    def handler() -> Outcome:
        task = _lock(task_id, version)
        _refuse_final(task, "marked done")
        if task.status != "in_progress":
            raise Conflict(
                f"Task WO-{task.pk:04d} is {STATUS_WORDS[task.status]}; start it before marking it done"
            )
        if not _is_manager(actor) and not _owns(actor, task):
            raise PermissionDenied(
                "Only the person this task is assigned to (or the ops lead) can mark it done"
            )
        if len(note) > 300:
            raise Invalid("The note is too long (up to 300 characters)")
        changes: dict[str, Any] = {"status": "done", "finished_at": timezone.now()}
        if note:
            changes["close_reason"] = note
        return _save(task, actor, **changes)

    return run_command(actor, "hub.complete_task", permission=WORK, payload=payload, handler=handler)


def cancel_task(actor: Any, *, task_id: int, reason: str, version: int | None = None) -> dict[str, Any]:
    reason = reason.strip()
    payload = {"task_id": task_id, "version": version, "reason": reason}

    def handler() -> Outcome:
        task = _lock(task_id, version)
        _refuse_final(task, "cancelled")
        if not reason or len(reason) > 300:
            raise Invalid("Give a short reason for cancelling (up to 300 characters)")
        if not _is_manager(actor):
            own_creation = task.created_by_id == actor.pk and task.source_kind == "manual"
            resolved_and_mine = task.source_cleared_at is not None and _owns(actor, task)
            if not ((own_creation and _owns(actor, task)) or resolved_and_mine):
                raise PermissionDenied("Only the ops lead or engineering can cancel this task")
        return _save(task, actor, status="cancelled", finished_at=timezone.now(), close_reason=reason)

    return run_command(actor, "hub.cancel_task", permission=WORK, payload=payload, handler=handler)
