"""Router contracts (LLD 07): intents, the classifier protocol and the tunable configuration."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence


class Intent(str, Enum):
    COURSE_QUESTION = "course_question"
    CHITCHAT = "chitchat"
    OUT_OF_SCOPE = "out_of_scope"
    SENSITIVE = "sensitive"  # grade disputes, complaints, personal circumstances, wellbeing (LLD 07 s4)
    UNCLEAR = "unclear"


@dataclass(frozen=True)
class IntentVerdict:
    intent: Intent
    confidence: float  # 0..1; only comparable within one classifier version
    version: str


class IntentClassifier(Protocol):
    version: str
    needs_embedding: bool

    def classify(self, text: str, embedding: Sequence[float] | None) -> IntentVerdict: ...


@dataclass(frozen=True)
class RouterConfig:
    # --- tier 2 (all PLACEHOLDERS until tuned on a labelled set; LLD 07 s9) ---
    sensitive_threshold: float = 0.35  # deliberately LOW: recall matters more than precision for human cases (s4)
    redirect_threshold: float = 0.60  # chitchat / out_of_scope below this fall through to the LLM path
    # --- tier 3 ---
    strong_threshold: float = 0.60  # complexity score at or above this -> strong model (s5)
    # --- canned texts (tenant-editable copy; the safety text in particular needs review by someone qualified) ---
    greeting_reply: str = "Hello! Ask me anything about your course notes."
    thanks_reply: str = "You're welcome! Ask me if there is anything else in your course notes."
    help_reply: str = ("I'm StudyBuddy. I answer questions using your course notes and show which notes I used. "
                       "If I can't find something in them, I'll say so, and I can pass tricky questions to your tutor.")
    redirect_reply: str = "I can only help with questions about your course notes. Try asking about a topic from the course."
    clarify_reply: str = "Could you tell me a bit more about what you'd like to know from your course notes?"
    safety_reply: str = ("I'm really sorry you're going through this. You don't have to handle it alone. "
                         "If you might be in danger, please contact your local emergency number right now, or reach "
                         "out to a crisis line or someone you trust. I've also let a member of staff know so someone "
                         "can follow up with you here.")
    unclear_action: str = "cheap"  # "cheap" (default route, retrieval will say if nothing matches) | "clarify"
