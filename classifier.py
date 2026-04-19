"""
classifier.py

Classifies a customer support ticket into one of four issue types using
weighted keyword pattern matching, and produces a normalised confidence score.

Issue types: refund | order_issue | product_issue | general_inquiry
"""

import re
from dataclasses import dataclass
from typing import Literal

IssueType = Literal["refund", "order_issue", "product_issue", "general_inquiry"]

_PATTERNS: dict[str, list[tuple[str, float]]] = {
    "refund": [
        (r"\brefund\b",               0.45),
        (r"\bmoney back\b",           0.40),
        (r"\bchargeback\b",           0.40),
        (r"\bcancel(led|lation)?\b",  0.40),
        (r"\breturn\b",               0.30),
        (r"\bnot (happy|satisfied)\b",0.15),
        (r"\bwant.*money\b",          0.30),
        (r"\bpaid.*not received\b",   0.35),
    ],
    "order_issue": [
        (r"\border\b",                              0.20),
        (r"\bshipping\b",                           0.30),
        (r"\bdelivery\b",                           0.30),
        (r"\bnot (arrived|delivered|received|shipped)\b", 0.35),
        (r"\btracking\b",                           0.30),
        (r"\bwhere is\b",                           0.25),
        (r"\bstill waiting\b",                      0.25),
        (r"\bdelayed\b",                            0.30),
        (r"\bwrong (item|product|address|color|colour|size|type)\b", 0.45),
        (r"\binvaild\b",                            0.20),
        (r"\bmissing\b",                            0.25),
        (r"\bpackage\b",                            0.20),
        (r"\bdidn't receive\b",                     0.30),
    ],
    "product_issue": [
        (r"\bbroken\b",              0.40),
        (r"\bdamaged\b",             0.40),
        (r"\bdefective\b",           0.45),
        (r"\bnot work(ing)?\b",      0.40),
        (r"\bdoes(n't|nt| not) work\b", 0.45),
        (r"\bstopped working\b",     0.45),
        (r"\bfaulty\b",              0.40),
        (r"\bmalfunctioning\b",      0.45),
        (r"\bquality\b",             0.20),
        (r"\bpoor\b",                0.15),
        (r"\bwon't turn on\b",       0.40),
        (r"\bcracked\b",             0.35),
    ],
    "general_inquiry": [
        (r"\bquestion\b",                      0.30),
        (r"\binfo(rmation)?\b",                0.30),
        (r"\bhow (do|can|to)\b",               0.30),
        (r"\bwhen will\b",                     0.25),
        (r"\bwhat is\b",                       0.25),
        (r"\bpolicy\b",                        0.40),
        (r"\bexchange\b",                      0.30),
        (r"\bplease (let me know|advise|confirm)\b", 0.25),
        (r"\bstatus\b",                        0.18),
        (r"\btell me about\b",                 0.25),
    ],
}


@dataclass
class ClassificationResult:
    """Output of the ticket classifier."""
    issue_type: IssueType
    confidence: float
    scores: dict[str, float]


def classify_ticket(subject: str, body: str) -> ClassificationResult:
    """
    Score each issue type using weighted regex matches on the combined
    subject and body text, then return the top category with a normalised
    confidence score in [0, 1].

    A raw score total below 0.15 means the text is too sparse to classify
    reliably; the function returns general_inquiry with confidence 0.35 so
    the agent will escalate.
    """
    text = re.sub(r"[^\w\s']", " ", (subject + " " + body).lower())

    raw: dict[str, float] = {category: 0.0 for category in _PATTERNS}
    for category, patterns in _PATTERNS.items():
        for pattern, weight in patterns:
            raw[category] += len(re.findall(pattern, text)) * weight

    total = sum(raw.values()) or 1e-9

    if total < 0.15:
        return ClassificationResult(
            issue_type="general_inquiry",
            confidence=0.35,
            scores={k: 0.0 for k in _PATTERNS},
        )

    normalised = {k: round(v / total, 3) for k, v in raw.items()}
    best: IssueType = max(normalised, key=lambda k: normalised[k])  # type: ignore[assignment]

    confidence = normalised[best]
    second_best = sorted(normalised.values(), reverse=True)[1]
    if confidence > second_best * 2:
        confidence = min(confidence * 1.15, 1.0)

    return ClassificationResult(
        issue_type=best,
        confidence=round(confidence, 3),
        scores=normalised,
    )
