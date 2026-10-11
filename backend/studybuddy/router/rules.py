"""Tier 1: deterministic rules (LLD 07 s3). Highest-risk phrases are checked FIRST and never depend on a model (s6).

All phrase lists are PLACEHOLDERS: small, auditable, biased toward sending people to a human. They will over-trigger
(a psychology question quoting the word "suicide" is routed to a person) and they will miss paraphrases; the
classifier tier and real traffic are meant to widen coverage. Every rule has an id so decisions can be audited.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..guardrails.text import skeleton
from ..pipeline.types import Route, RouteDecision
from .types import RouterConfig


def prep(text: str) -> str:
    """Lowercase, look-alike-free, curly quotes straightened, punctuation turned into spaces."""
    t = skeleton(text).replace("’", "'").replace("`", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern


def _rules(prefix: str, *patterns: str) -> list[Rule]:
    return [Rule(f"{prefix}.{i}", re.compile(p)) for i, p in enumerate(patterns, 1)]


# Wellbeing / safety: always first. A match creates a ticket AND shows a supportive message, never an LLM answer.
SAFETY = _rules(
    "safety",
    r"\b(kill|hurt|harm|cut) (myself|my self)\b",
    r"\b(suicide|suicidal|self harm|selfharm)\b",
    r"\b(end|take) my (own )?life\b",
    r"\bi (want|wanna|need|am going|am thinking|'m going|'m thinking)( of| about)? (to )?(die|disappear|end it)\b",
    r"\b(don't|do not|dont) want to (live|be alive|be here anymore)\b",
    r"\b(thinking|thought|thoughts|think) (of|about) (ending|killing|hurting) (it|myself|my life|things)\b",
    r"\b(ending|end) it all\b",
    r"\b(no reason to live|better off dead|can't go on|cant go on)\b",
)
# Harassment, discrimination, legal threats and formal complaints: a person must read these.
ESCALATE = _rules(
    "escalate",
    r"\b(harass\w*|bully\w*|stalk\w*|discriminat\w*|assault\w*|abus\w+)\b",
    r"\b(lawyer|solicitor|attorney|sue|sue you|legal action|lawsuit|take (this|it) to court)\b",
    r"\b(file|make|lodge|submit|want to make|want to file) (a |an )?(formal )?(complaint|grievance)\b",
    r"\b(dispute|appeal|contest|challenge) (my|the|this|a) (grade|grades|mark|marks|score|result|decision)\b",
    r"\b(my|the) (grade|mark|score|result) (is|was|seems) (wrong|unfair|incorrect|a mistake)\b",
    r"\b(deferral|mitigating circumstances|special consideration)\b",
    r"\b(ask|asking|apply|applying|request|requesting|need|want|get|getting) (for )?(an? )?extension\b",
    r"\bextension (on|for|to) (my|the|this) (assignment|essay|deadline|coursework|exam|project|paper)\b",
    r"\b(family emergency|medical emergency|death in the family|passed away|in hospital|diagnosed with)\b",
    r"\b(refund|cancel my (enrolment|enrollment|account|subscription|registration)|withdraw from the university)\b",
)
GREETING = re.compile(r"^(hi|hello|hey|hiya|howdy|good (morning|afternoon|evening)|yo|hi there|hello there|hey there)( (studybuddy|buddy|there))?$")
THANKS = re.compile(r"^(thanks|thank you|thx|ty|cheers|thanks a lot|thank you so much|many thanks|much appreciated)( (studybuddy|buddy))?$")
GOODBYE = re.compile(r"^(bye|goodbye|see you|see ya|cya|good night|talk later|that's all|that is all)$")
HELP = re.compile(r"^(help|what can you do|who are you|what are you|how do you work|how does this work|what is studybuddy)$")


def tier1(text: str, cfg: RouterConfig) -> RouteDecision | None:
    """Return a decision, or None to fall through to the classifier. Safety is evaluated before anything else."""
    t = prep(text)
    if not t:
        return RouteDecision(Route.REDIRECT, False, "rule:empty", canned_answer=cfg.clarify_reply, confidence=1.0)
    for rule in SAFETY:
        if rule.pattern.search(t):
            return RouteDecision(Route.HUMAN, False, f"rule:{rule.id}", canned_answer=cfg.safety_reply, confidence=1.0)
    for rule in ESCALATE:
        if rule.pattern.search(t):
            return RouteDecision(Route.HUMAN, False, f"rule:{rule.id}", confidence=1.0)
    for name, pattern, reply in (("greeting", GREETING, cfg.greeting_reply), ("thanks", THANKS, cfg.thanks_reply),
                                 ("goodbye", GOODBYE, "Good luck with your studies! I'm here whenever you need me."),
                                 ("help", HELP, cfg.help_reply)):
        if pattern.match(t):
            return RouteDecision(Route.RULE, False, f"rule:{name}", canned_answer=reply, confidence=1.0)
    return None
