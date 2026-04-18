"""
tools.py

Simulated external tools for the customer support agent.
Each tool applies realistic async latency and randomised transient failures
to demonstrate retry and escalation behaviour.

Data is loaded DYNAMICALLY inside each tool call (not at module import time)
so the module is safe under Streamlit re-runs, hot-reload, and any working
directory. BASE_DIR / DATA_DIR are resolved once from __file__ so they are
always correct regardless of the process CWD.
"""

import asyncio
import json
import logging
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ── Path resolution ────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


# ── Data loading helpers ───────────────────────────────────────────────────────

def load_json(filename: str) -> dict:
    """Load and return a JSON file from DATA_DIR. Raises clearly if missing."""
    path = DATA_DIR / filename
    if not path.exists():
        msg = f"[tools] MISSING FILE: {filename} — searched at {path}"
        logging.error(msg)
        raise FileNotFoundError(f"{filename} NOT FOUND at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_data() -> tuple[dict, dict, dict]:
    """
    Load and return (customers, orders, products) from the data directory.

    Called inside every tool function so data is always fresh and the
    correct path is used regardless of the current working directory or
    Streamlit's module caching behaviour.
    """
    customers = load_json("customers.json")
    orders    = load_json("orders.json")
    products  = load_json("products.json")
    print(
        f"[tools] get_data() -> {len(customers)} customers, "
        f"{len(orders)} orders, {len(products)} products  "
        f"(from {DATA_DIR})"
    )
    return customers, orders, products


def get_knowledge_base() -> str:
    """Load and return the knowledge-base markdown text."""
    kb_path = DATA_DIR / "knowledge-base.md"
    if not kb_path.exists():
        msg = f"[tools] MISSING FILE: knowledge-base.md — searched at {kb_path}"
        logging.error(msg)
        raise FileNotFoundError(f"knowledge-base.md NOT FOUND at {kb_path}")
    with open(kb_path, "r", encoding="utf-8") as f:
        content = f.read()
    print(f"[tools] get_knowledge_base() → {len(content)} chars")
    return content


# ── Refund state (per-process, intentionally global) ──────────────────────────
REFUND_POLICY_DAYS: int = 30
_issued_refunds: set[str] = set()


# ── Exceptions ────────────────────────────────────────────────────────────────

class ToolError(Exception):
    """Raised for deterministic, non-retryable tool failures (e.g. not found)."""


class ToolTimeout(Exception):
    """Raised for transient failures (network timeout, service unavailable)."""


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _latency(base: float = 0.3, jitter: float = 0.3) -> None:
    await asyncio.sleep(base + random.uniform(0, jitter))


def _transient_fail(rate: float) -> bool:
    return random.random() < rate


# ── Public tool functions ─────────────────────────────────────────────────────

async def get_customer(email: str) -> dict[str, Any]:
    """Return customer profile for the given email address."""
    await _latency(0.2, 0.2)
    if _transient_fail(0.12):
        raise ToolTimeout(f"get_customer timed out for {email!r}")

    customers, _, _ = get_data()
    print(f"[tools] get_customer({email!r}) — searching {len(customers)} customers")

    customer = customers.get(email)
    if customer is None:
        print(f"[tools] get_customer({email!r}) - Not found. Returning fallback.")
        return {"status": "error", "customer": {"email": email, "name": "Unknown User", "tier": "standard", "id": "UNKNOWN"}}
    return {"status": "ok", "customer": customer}


async def get_order(order_id: str) -> dict[str, Any]:
    """Return order details for the given order ID."""
    await _latency(0.2, 0.3)
    if _transient_fail(0.10):
        raise ToolTimeout(f"get_order timed out for {order_id!r}")

    _, orders, _ = get_data()
    print(f"[tools] get_order({order_id!r}) — searching {len(orders)} orders")

    order = orders.get(order_id)
    if order is None:
        print(f"[tools] get_order({order_id!r}) - Not found. Returning structured error.")
        return {"status": "error", "error": f"Order {order_id!r} not found", "order": {"status": "not_found", "product": "Unknown", "amount": 0.0, "order_date": "1970-01-01T00:00:00Z"}}
    return {"status": "ok", "order": order}


async def check_refund_eligibility(order_id: str) -> dict[str, Any]:
    """Evaluate whether an order is eligible for a refund under current policy."""
    await _latency(0.3, 0.2)
    if _transient_fail(0.10):
        raise ToolTimeout(f"check_refund_eligibility timed out for {order_id!r}")

    _, orders, _ = get_data()
    order = orders.get(order_id)
    if order is None:
        return {"status": "error", "eligible": False, "reasons": ["Order not found"], "amount": None}

    eligible = True
    reasons: list[str] = []

    if order_id in _issued_refunds:
        eligible = False
        reasons.append("Refund already issued for this order.")
    if order["status"] != "delivered":
        eligible = False
        reasons.append(f"Order status is '{order['status']}'; only delivered orders may be refunded.")
    if order["order_date"]:
        age_days = (datetime.now() - datetime.fromisoformat(order["order_date"])).days
        if age_days > REFUND_POLICY_DAYS:
            eligible = False
            reasons.append(f"Order is {age_days} days old; refund window is {REFUND_POLICY_DAYS} days.")

    return {
        "status":   "ok",
        "order_id": order_id,
        "eligible": eligible,
        "reasons":  reasons,
        "amount":   order["amount"] if eligible else None,
    }


async def issue_refund(order_id: str) -> dict[str, Any]:
    """Process and record a refund for the given order."""
    await _latency(0.4, 0.3)
    if _transient_fail(0.08):
        raise ToolTimeout(f"issue_refund timed out for {order_id!r}")

    _, orders, _ = get_data()
    order = orders.get(order_id)
    if order is None:
        return {"status": "error", "error": "Order not found"}
    if order_id in _issued_refunds:
        raise ToolError(f"Refund already issued for order {order_id!r}")

    _issued_refunds.add(order_id)
    return {
        "status":             "ok",
        "refund_id":          f"REF-{uuid.uuid4().hex[:8].upper()}",
        "order_id":           order_id,
        "amount":             order["amount"],
        "estimated_arrival":  (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"),
    }


async def send_reply(ticket_id: str, message: str) -> dict[str, Any]:
    """Send an email reply to the customer associated with the ticket."""
    await _latency(0.2, 0.2)
    if _transient_fail(0.08):
        raise ToolTimeout(f"send_reply timed out for ticket {ticket_id!r}")
    return {
        "status":          "ok",
        "ticket_id":       ticket_id,
        "message_preview": message[:120] + ("..." if len(message) > 120 else ""),
        "sent_at":         datetime.now().isoformat(),
    }


async def escalate(ticket_id: str, summary: str) -> dict[str, Any]:
    """Escalate a ticket to the human support team with a structured summary."""
    await _latency(0.2, 0.1)
    if _transient_fail(0.05):
        raise ToolTimeout(f"escalate timed out for ticket {ticket_id!r}")
    return {
        "status":          "ok",
        "escalation_id":   f"ESC-{uuid.uuid4().hex[:6].upper()}",
        "ticket_id":       ticket_id,
        "assigned_to":     "Human Support Team",
        "summary_preview": summary[:120] + ("..." if len(summary) > 120 else ""),
        "escalated_at":    datetime.now().isoformat(),
    }
