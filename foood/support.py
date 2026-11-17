"""Support: the human in the loop.

The customer app has no approval screen and the apps stay unchanged, so when an
engine needs a person it asks support. An engine posts a ``Question`` for an
order (a prompt with options or a typed form, a default and a deadline); support
answers from the page at ``/support``; the answer, or the default when the
deadline passes, reaches the engine as a ``support.answered`` event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from foood.auth import CurrentUser, StoreDep
from foood.errors import ApiError
from foood.models import new_id
from foood.store import Store, User, iso, now

router = APIRouter(tags=["support"])

PAGE = Path(__file__).with_name("support.html")
DEFAULT_DEADLINE = timedelta(minutes=5)


@dataclass
class Question:
    order_id: str
    kind: str
    prompt: str
    options: list[dict[str, str]] | None = None  # [{"value": ..., "label": ...}]
    form: dict[str, Any] | None = None  # a JSON schema for a typed answer
    default: Any = None
    deadline: datetime = field(default_factory=lambda: now() + DEFAULT_DEADLINE)
    context: dict[str, Any] = field(default_factory=dict)
    status: str = "OPEN"  # OPEN | ANSWERED | EXPIRED
    answer: Any = None
    answered_by: str | None = None
    answered_at: datetime | None = None
    id: str = field(default_factory=new_id)
    asked_at: datetime = field(default_factory=now)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "orderId": self.order_id,
            "kind": self.kind,
            "prompt": self.prompt,
            "options": self.options,
            "form": self.form,
            "default": self.default,
            "deadline": iso(self.deadline),
            "context": dict(self.context),
            "status": self.status,
            "answer": self.answer,
            "answeredBy": self.answered_by,
            "answeredAt": iso(self.answered_at) if self.answered_at else None,
            "askedAt": iso(self.asked_at),
        }


def ask(
    store: Store,
    order_id: str,
    kind: str,
    prompt: str,
    *,
    options: list[dict[str, str]] | None = None,
    form: dict[str, Any] | None = None,
    default: Any = None,
    deadline_seconds: float = DEFAULT_DEADLINE.total_seconds(),
    **context: Any,
) -> Question:
    """An engine asks support; the answer arrives later as ``support.answered``."""
    if options is None and form is None:
        raise ValueError("a question needs options or a form")
    question = Question(
        order_id,
        kind,
        prompt,
        options=options,
        form=form,
        default=default,
        deadline=now() + timedelta(seconds=deadline_seconds),
        context=context,
    )
    store.questions[question.id] = question
    store.publish("support.asked", question=question)
    return question


def validate_answer(question: Question, value: Any) -> None:
    if question.options is not None:
        allowed = [o["value"] for o in question.options]
        if value not in allowed:
            raise ApiError(400, f"answer must be one of {allowed}")
    if question.form is not None:
        try:
            jsonschema.validate(value, question.form)
        except jsonschema.ValidationError as exc:
            raise ApiError(400, f"invalid answer: {exc.message}") from exc


def answer(store: Store, question: Question, value: Any, by: str) -> Question:
    if question.status != "OPEN":
        raise ApiError(409, f"This question is already {question.status.lower()}", error="Conflict")
    validate_answer(question, value)
    question.status = "ANSWERED"
    question.answer = value
    question.answered_by = by
    question.answered_at = now()
    store.publish("support.answered", question=question)
    return question


def expire_due(store: Store) -> list[Question]:
    """Past their deadline, open questions take the default the engine declared."""
    moment = now()
    expired = []
    for question in store.questions.values():
        if question.status == "OPEN" and question.deadline <= moment:
            question.status = "EXPIRED"
            question.answer = question.default
            question.answered_by = "deadline"
            question.answered_at = moment
            expired.append(question)
            store.publish("support.answered", question=question)
    return expired


def install(store: Store) -> None:
    def sweep() -> None:
        expire_due(store)

    store.tickers.append(sweep)


def plan_panel(decisions: list[dict[str, Any]], waiting_at: str | None) -> dict[str, Any] | None:
    """What the page shows of an order's plan: the route, where it stands, why it changed.

    Built only from the decisions every engine records; the hand-wired engine plans nothing.
    """
    plans = [d for d in decisions if d["decision"] in {"plan", "replan", "plan skips ahead"}]
    if not plans:
        return None
    current = plans[-1]
    steps = list(current.get("steps") or [])
    position = steps.index(waiting_at) if waiting_at in steps else None
    replans = [d for d in plans if d["decision"] == "replan"]
    reasoning = [d for d in decisions if str(d["decision"]).startswith("llm: ")]
    return {
        "steps": steps,
        "cost": current.get("planCost"),
        "position": position,
        "waitingAt": waiting_at,
        "replans": len(replans),
        "lastReplan": replans[-1].get("because") if replans else None,
        "considered": current.get("considered") or [],
        "lastReasoning": reasoning[-1].get("because") if reasoning else None,
        "madeAt": current.get("at"),
    }


# --- routes ------------------------------------------------------------------------------------


class Answer(BaseModel):
    value: Any


def _support_only(user: User) -> None:
    if user.role != "ADMIN":
        raise ApiError(403, "Support only")


@router.get("/api/support/questions")
async def list_questions(
    user: CurrentUser, store: StoreDep, status: str = "open"
) -> dict[str, Any]:
    _support_only(user)
    rows = list(store.questions.values())
    if status == "open":
        rows = [q for q in rows if q.status == "OPEN"]
    rows.sort(key=lambda q: q.asked_at, reverse=True)
    return {"items": [q.to_json() for q in rows], "total": len(rows)}


@router.post("/api/support/questions/{id}/answer")
async def answer_question(
    id: str, body: Answer, user: CurrentUser, store: StoreDep
) -> dict[str, Any]:
    _support_only(user)
    question = store.questions.get(id)
    if question is None:
        raise ApiError(404, "Question not found")
    return answer(store, question, body.value, user.id).to_json()


@router.get("/api/support/overview")
async def overview(
    request: Request, user: CurrentUser, store: StoreDep, limit: int = 20
) -> dict[str, Any]:
    """What the support page shows: open questions, recent orders and decisions, the scenario."""
    _support_only(user)
    orders = sorted(store.orders.values(), key=lambda o: o.created_at, reverse=True)[:limit]
    engine = getattr(request.app.state, "engine", None)
    rows = []
    for order in orders:
        row = order.to_json(store)
        restaurant = store.restaurants.get(order.restaurant_id)
        row["restaurantName"] = restaurant.name if restaurant else None
        decisions = store.decisions.get(order.id, [])
        if order.replaces_order_id and not decisions:  # a replacement shares its original's thread
            decisions = store.decisions.get(order.replaces_order_id, [])
        row["decisions"] = decisions
        waiting_at = getattr(engine, "waiting_at", None)
        row["waitingAt"] = waiting_at(order.id) if callable(waiting_at) else None
        row["plan"] = plan_panel(decisions, row["waitingAt"])
        rows.append(row)
    open_questions = [q.to_json() for q in store.questions.values() if q.status == "OPEN"]
    return {
        "engine": engine.name if engine else None,
        "scenario": store.engine.status() if store.engine else None,
        "openQuestions": sorted(open_questions, key=lambda q: q["askedAt"]),
        "orders": rows,
        "now": iso(now()),
    }


@router.get("/support", response_class=HTMLResponse, include_in_schema=False)
async def support_page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))
