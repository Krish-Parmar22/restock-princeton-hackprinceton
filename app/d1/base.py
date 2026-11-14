"""d1 decision-model interface. Every caller (grab checks, agent tools, the guard) uses this.

d1 answers narrow questions with a probability per option. It never generates text.
Backends: 'fake' (deterministic, for tests and offline dev) and 'liquid' (Liquid AI API,
implemented in app/d1/liquid.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

QuestionType = Literal["noul", "choice"]  # noul = yes/no probability; choice = pick one option


@dataclass(frozen=True)
class D1Question:
    id: str                       # for our code only; not sent to the model as meaning
    type: QuestionType
    instructions: str             # the full question, self-contained
    options: tuple[str, ...] = ()  # required for 'choice'; ignored for 'noul'


@dataclass
class D1Answer:
    answer: str                   # chosen option, or 'yes'/'no' for noul
    probs: dict[str, float]       # probability per option ('yes'/'no' for noul)
    confidence: float = field(default=0.0)  # probability of the chosen answer

    @property
    def p_yes(self) -> float:
        return self.probs.get("yes", 0.0)


class D1Client(Protocol):
    def ask(
        self,
        questions: list[D1Question],
        state: str | dict,
        images: list[Path] | None = None,
    ) -> dict[str, D1Answer]:
        """Ask several independent questions about one state (+ images) in one call.
        Returns {question.id: D1Answer}."""
        ...


class FakeD1:
    """Deterministic stand-in. Picks the first option (or 'yes') with probability `p`,
    unless `overrides` maps question id -> (answer, prob)."""

    def __init__(self, p: float = 0.9, overrides: dict[str, tuple[str, float]] | None = None):
        self.p = p
        self.overrides = overrides or {}

    def ask(self, questions, state, images=None):
        out: dict[str, D1Answer] = {}
        for q in questions:
            opts = list(q.options) if q.type == "choice" else ["yes", "no"]
            ans, p = self.overrides.get(q.id, (opts[0], self.p))
            rest = [o for o in opts if o != ans]
            share = (1 - p) / len(rest) if rest else 0.0
            probs = {ans: p, **{o: share for o in rest}}
            out[q.id] = D1Answer(answer=ans, probs=probs, confidence=p)
        return out


def get_client() -> D1Client:
    from app.config import settings

    if settings.d1_backend == "openrouter":
        from app.d1.openrouter import OpenRouterD1

        return OpenRouterD1(api_key=settings.openrouter_api_key)
    if settings.d1_backend == "liquid":
        from app.d1.liquid import LiquidD1  # implemented by the d1 workstream

        return LiquidD1(api_key=settings.liquid_api_key)
    if settings.d1_backend == "fake":
        return FakeD1()
    raise ValueError(f"unknown D1_BACKEND {settings.d1_backend!r}: use openrouter, liquid or fake")
