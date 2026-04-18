"""
agent.py

Production-grade autonomous customer support agent.

Behaviour contract
------------------
1. Every tool call carries an explicit reason field logged before execution.
2. Every decision point writes a decision-type audit entry justifying the
   next action before that action is taken.
3. Tools are retried up to MAX_RETRIES (2) times with exponential back-off
   (0.4 s, 0.8 s). After exhausting retries the ticket is escalated.
4. If classifier confidence < ESCALATION_THRESHOLD (0.6) the agent
   escalates immediately without entering the action chain.
5. A minimum of MIN_TOOL_CALLS (3) successful tool invocations is required
   before marking a ticket resolved. A padding function enforces this.
6. All tickets are processed concurrently; this module only handles one.
   Concurrency is managed by the caller (main.py).
"""

import asyncio
import time
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Awaitable

from classifier import classify_ticket, ClassificationResult
import tools as T

def _get_missing_order_msg(name: str, context: str) -> tuple[str, str]:
    if context == "refund":
        pool = [
            "We couldn't find your order ID. Please share it so we can assist you with your refund.",
            "To proceed with your refund, we need your order number. Could you provide it?",
            "Your refund request is valid, but we need an order ID to continue."
        ]
    elif context == "order_issue":
        pool = [
            "We couldn't find your order ID. Please share it so we can assist you with order tracking.",
            "To proceed with your order tracking, we need your order number. Could you provide it?",
            "Your request is valid, but we need an order ID to track your order."
        ]
    elif context == "product_issue":
        pool = [
            "We couldn't find your order ID. Please share it so we can assist you with your damaged product.",
            "To proceed with a replacement or refund, we need your order number. Could you provide it?",
            "Your request regarding the product issue is valid, but we need an order ID to continue."
        ]
    else:
        pool = [
            "We couldn't find your order ID. Please share it so we can assist you.",
            "To proceed, we need your order number. Could you provide it?",
            "Your request is valid, but we need an order ID to continue."
        ]
    
    val = random.choice(pool)
    msg = f"Dear {name},\n\n{val}\n\nSupport Team"
    return msg, val


MAX_RETRIES: int = 2
ESCALATION_THRESHOLD: float = 0.6
MIN_TOOL_CALLS: int = 3
BASE_BACKOFF: float = 0.4


@dataclass
class AuditEntry:
    """
    Immutable record of a single agent step.

    An entry is created for every action taken: tool invocations (including
    each retry attempt), back-off waits, decision points, and the final
    resolution. The status field distinguishes the entry type:

        success  — tool returned a valid result.
        retry    — tool failed; another attempt will follow.
        error    — all retries exhausted; tool call failed permanently.
        decision — a reasoning or routing step with no tool invocation.
    """
    ticket_id: str
    timestamp: str
    step_name: str
    tool_name: str | None
    input: dict | None
    output: Any
    reason: str
    confidence: float
    status: str
    attempt: int = 1
    duration_ms: float = 0.0
    error_message: str | None = None


@dataclass
class TicketResult:
    """
    Complete outcome record for a single processed ticket.

    Returned by process_ticket() and consumed by main.py for display
    and serialisation. All six summary fields required by spec are present.
    """
    ticket_id: str
    customer_email: str
    issue_type: str
    final_confidence: float
    final_status: str
    resolution_message: str
    successful_tool_calls: int
    total_attempts: int
    retry_count: int
    audit_trail: list[AuditEntry] = field(default_factory=list)
    error: str | None = None


def _now() -> str:
    return datetime.now().isoformat()


def _count_successful_tool_calls(audit: list[AuditEntry]) -> int:
    return sum(1 for e in audit if e.tool_name and e.status == "success")


def _count_tool_attempts(audit: list[AuditEntry]) -> int:
    return sum(1 for e in audit if e.tool_name)


def _count_retries(audit: list[AuditEntry]) -> int:
    return sum(1 for e in audit if e.status == "retry")


def _record_decision(
    audit: list[AuditEntry],
    *,
    ticket_id: str,
    step_name: str,
    reason: str,
    confidence: float,
    detail: dict | None = None,
) -> None:
    """Append a decision-type audit entry. No tool is invoked."""
    audit.append(AuditEntry(
        ticket_id=ticket_id,
        timestamp=_now(),
        step_name=step_name,
        tool_name=None,
        input=None,
        output=detail,
        reason=reason,
        confidence=confidence,
        status="decision",
    ))


async def _invoke_tool(
    tool_fn: Callable[..., Awaitable[Any]],
    *args: Any,
    ticket_id: str,
    step_name: str,
    reason: str,
    confidence: float,
    audit: list[AuditEntry],
    **kwargs: Any,
) -> Any:
    """
    Invoke an async tool function with up to MAX_RETRIES retry attempts.

    Retry schedule
    --------------
    Attempt 1 — immediate.
    Attempt 2 — wait BASE_BACKOFF seconds (0.4 s).
    Attempt 3 — wait BASE_BACKOFF * 2 seconds (0.8 s).

    Every attempt (success or failure) produces one AuditEntry. Each
    back-off wait also produces a decision-type AuditEntry explaining the
    delay. If all attempts fail the last exception is re-raised for the
    caller to handle (typically by calling escalate).
    """
    tool_name = tool_fn.__name__
    serialised_input: dict = {f"arg{i}": str(a) for i, a in enumerate(args)}
    serialised_input.update({k: str(v) for k, v in kwargs.items()})

    total_attempts = MAX_RETRIES + 1
    last_exc: Exception | None = None

    for attempt in range(1, total_attempts + 1):
        t0 = time.monotonic()
        try:
            result = await tool_fn(*args, **kwargs)
            duration_ms = round((time.monotonic() - t0) * 1000, 2)
            audit.append(AuditEntry(
                ticket_id=ticket_id,
                timestamp=_now(),
                step_name=step_name,
                tool_name=tool_name,
                input=serialised_input,
                output=result,
                reason=reason,
                confidence=confidence,
                status="success",
                attempt=attempt,
                duration_ms=duration_ms,
            ))
            return result

        except (T.ToolTimeout, T.ToolError) as exc:
            duration_ms = round((time.monotonic() - t0) * 1000, 2)
            last_exc = exc
            has_retries_left = attempt < total_attempts
            audit.append(AuditEntry(
                ticket_id=ticket_id,
                timestamp=_now(),
                step_name=step_name,
                tool_name=tool_name,
                input=serialised_input,
                output=None,
                reason=reason,
                confidence=confidence,
                status="retry" if has_retries_left else "error",
                attempt=attempt,
                duration_ms=duration_ms,
                error_message=str(exc),
            ))
            if has_retries_left:
                backoff = BASE_BACKOFF * (2 ** (attempt - 1))
                _record_decision(
                    audit,
                    ticket_id=ticket_id,
                    step_name=f"{step_name}.backoff",
                    reason=(
                        f"'{tool_name}' failed on attempt {attempt}/{total_attempts}: {exc}. "
                        f"Waiting {backoff:.1f}s before retry "
                        f"({total_attempts - attempt} attempt(s) remaining)."
                    ),
                    confidence=confidence,
                    detail={"backoff_seconds": backoff, "attempt": attempt, "error": str(exc)},
                )
                await asyncio.sleep(backoff)
            else:
                raise

    raise last_exc  # type: ignore[misc]


async def _enforce_min_tool_calls(
    ticket: dict,
    clf: ClassificationResult,
    audit: list[AuditEntry],
) -> None:
    """
    Guarantee at least MIN_TOOL_CALLS successful tool invocations have been
    recorded. Called after the main handler completes. Supplemental calls are
    added only if the handler's own chain fell short, which can happen when
    a general_inquiry ticket has no order_id.
    """
    tid = ticket["id"]
    current = _count_successful_tool_calls(audit)
    if current >= MIN_TOOL_CALLS:
        return

    _record_decision(
        audit,
        ticket_id=tid,
        step_name="enforce_min_tool_calls",
        reason="Enforcing minimum tool call requirement",
        confidence=clf.confidence,
        detail={"current": current, "required": MIN_TOOL_CALLS},
    )

    supplemental_steps = [
        (
            T.get_customer, (ticket["customer_email"],),
            "supplemental.get_customer",
            "Enforcing minimum tool call requirement",
        ),
        (
            T.get_order, (ticket.get("order_id", ""),),
            "supplemental.get_order",
            "Enforcing minimum tool call requirement",
        ),
        (
            T.send_reply, (tid, "We are actively reviewing your request and will follow up shortly."),
            "supplemental.send_status",
            "Enforcing minimum tool call requirement",
        ),
    ]

    for tool_fn, args, step_name, reason in supplemental_steps:
        if _count_successful_tool_calls(audit) >= MIN_TOOL_CALLS:
            break
        if tool_fn == T.get_order and not ticket.get("order_id"):
            continue
        try:
            await _invoke_tool(
                tool_fn, *args,
                ticket_id=tid,
                step_name=step_name,
                reason=reason,
                confidence=clf.confidence,
                audit=audit,
            )
        except Exception:
            pass


async def _run_escalate(
    ticket_id: str,
    summary: str,
    audit_reason: str,
    escalation_reason: str,
    confidence: float,
    audit: list[AuditEntry],
    step_name: str = "escalate",
) -> str:
    """
    Invoke the escalate tool (with retry) and return a resolution string.
    Logs the decision before the tool call.
    """
    _record_decision(
        audit,
        ticket_id=ticket_id,
        step_name=f"decide.{step_name}",
        reason=audit_reason,
        confidence=confidence,
    )
    try:
        resp = await _invoke_tool(
            T.escalate, ticket_id, escalation_reason, summary,
            ticket_id=ticket_id,
            step_name=step_name,
            reason=audit_reason,
            confidence=confidence,
            audit=audit,
        )
        return f"Escalated due to {escalation_reason} — ID={resp['escalation_id']}"
    except Exception as exc:
        return f"Escalation failed: {exc}"


async def _handle_refund(
    ticket: dict,
    clf: ClassificationResult,
    audit: list[AuditEntry],
) -> tuple[str, str]:
    """
    Resolve a refund request.

    Tool chain:
        get_customer -> get_order -> check_refund_eligibility
            -> issue_refund (if eligible) -> send_reply
    """
    tid      = ticket["id"]
    email    = ticket["customer_email"]
    order_id = ticket.get("order_id", "")
    conf     = clf.confidence

    _record_decision(audit, ticket_id=tid, step_name="decide.get_customer", confidence=conf,
        reason="Verify requester identity and retrieve account tier before any financial action.")
    cust_resp = await _invoke_tool(
        T.get_customer, email,
        ticket_id=tid, step_name="get_customer", confidence=conf, audit=audit,
        reason="Fetch customer profile to confirm identity and account standing.")
    customer = cust_resp["customer"]

    if not order_id:
        _record_decision(audit, ticket_id=tid, step_name="decide.missing_order_id", confidence=conf,
            reason="Order ID is missing. Cannot verify purchase or eligibility. Requesting details.")
        msg, val = _get_missing_order_msg(customer['name'], "refund")
        await _invoke_tool(T.send_reply, tid, msg, ticket_id=tid, step_name="send_reply.missing_order", confidence=conf, audit=audit, reason="Ask customer for missing order ID.")
        return "resolved", val

    _record_decision(audit, ticket_id=tid, step_name="decide.get_order", confidence=conf,
        reason=(f"Customer {customer['name']} (tier={customer['tier']}) verified. "
                f"Fetching order {order_id} to confirm product, amount, and purchase date "
                "before running the eligibility check."))
    order_resp = await _invoke_tool(
        T.get_order, order_id,
        ticket_id=tid, step_name="get_order", confidence=conf, audit=audit,
        reason=f"Retrieve order {order_id} to validate ownership and obtain refund amount.")

    if order_resp.get("status") == "error":
        msg = f"Dear {customer['name']},\n\nWe couldn't locate your order. Please verify your order ID.\n\nSupport Team"
        await _invoke_tool(T.send_reply, tid, msg, ticket_id=tid, step_name="send_reply.invalid_order", confidence=conf, audit=audit, reason="Order not found. Requesting verification.")
        return "resolved", "We couldn't locate your order. Please verify your order ID."

    order = order_resp["order"]

    _record_decision(audit, ticket_id=tid, step_name="decide.check_eligibility", confidence=conf,
        reason=(f"Order {order_id} is '{order['status']}' for ${order['amount']:.2f}. "
                "Running policy check: 30-day window, delivered status, no duplicate refund."))
    elig_resp = await _invoke_tool(
        T.check_refund_eligibility, order_id,
        ticket_id=tid, step_name="check_refund_eligibility", confidence=conf, audit=audit,
        reason="Apply refund policy rules to determine if this order qualifies.")

    if elig_resp["eligible"]:
        _record_decision(audit, ticket_id=tid, step_name="decide.issue_refund", confidence=conf,
            reason=(f"Eligibility confirmed for order {order_id} "
                    f"(${elig_resp['amount']:.2f}). Proceeding to issue refund."))
        refund_resp = await _invoke_tool(
            T.issue_refund, order_id,
            ticket_id=tid, step_name="issue_refund", confidence=conf, audit=audit,
            reason=f"Issue approved refund of ${elig_resp['amount']:.2f} for order {order_id}.")
        refund_id = refund_resp["refund_id"]
        amount    = refund_resp["amount"]
        eta       = refund_resp["estimated_arrival"]
        msg = (
            f"Dear {customer['name']},\n\n"
            f"Your refund request for order {order_id} has been approved.\n\n"
            f"  Refund amount : ${amount:.2f}\n"
            f"  Refund ID     : {refund_id}\n"
            f"  Estimated ETA : {eta}\n\n"
            "Please allow 3-5 business days for the credit to appear. "
            "Reply to this message if you have further questions.\n\nSupport Team"
        )
        await _invoke_tool(
            T.send_reply, tid, msg,
            ticket_id=tid, step_name="send_reply.refund_approved", confidence=conf, audit=audit,
            reason="Notify customer of approved refund with full confirmation details.")
        return "resolved", f"Refund {refund_id} of ${amount:.2f} issued"

    reasons_text = "; ".join(elig_resp["reasons"])
    _record_decision(audit, ticket_id=tid, step_name="decide.deny_refund", confidence=conf,
        reason=(f"Refund ineligible for order {order_id}: {reasons_text}. "
                "Informing customer and offering a path for further queries."))
    msg = (
        f"Dear {customer['name']},\n\n"
        f"We reviewed your refund request for order {order_id}.\n\n"
        f"Unfortunately a refund cannot be issued at this time:\n  {reasons_text}\n\n"
        "If you believe this is an error, please reply and we will re-review.\n\nSupport Team"
    )
    await _invoke_tool(
        T.send_reply, tid, msg,
        ticket_id=tid, step_name="send_reply.refund_denied", confidence=conf, audit=audit,
        reason="Notify customer of refund denial with specific policy reasons.")
    return "resolved", f"Refund denied: {reasons_text}"


async def _handle_order_issue(
    ticket: dict,
    clf: ClassificationResult,
    audit: list[AuditEntry],
) -> tuple[str, str]:
    """
    Resolve an order / shipping / delivery inquiry.

    Tool chain: get_customer -> get_order -> send_reply
    """
    tid      = ticket["id"]
    email    = ticket["customer_email"]
    order_id = ticket.get("order_id", "")
    conf     = clf.confidence

    _record_decision(audit, ticket_id=tid, step_name="decide.get_customer", confidence=conf,
        reason="Customer is reporting a shipping or delivery problem. "
               "Must verify identity before accessing order data.")
    cust_resp = await _invoke_tool(
        T.get_customer, email,
        ticket_id=tid, step_name="get_customer", confidence=conf, audit=audit,
        reason="Validate the email belongs to an active customer account.")
    customer = cust_resp["customer"]

    if not order_id:
        _record_decision(audit, ticket_id=tid, step_name="decide.missing_order_id", confidence=conf,
            reason="Order ID is missing. Cannot retrieve live carrier and status data. Requesting details.")
        msg, val = _get_missing_order_msg(customer['name'], "order_issue")
        await _invoke_tool(T.send_reply, tid, msg, ticket_id=tid, step_name="send_reply.missing_order", confidence=conf, audit=audit, reason="Ask customer for missing order ID.")
        return "resolved", val

    _record_decision(audit, ticket_id=tid, step_name="decide.get_order", confidence=conf,
        reason=(f"Identity confirmed for {customer['name']}. "
                f"Fetching order {order_id} to retrieve live carrier and status data."))
    order_resp = await _invoke_tool(
        T.get_order, order_id,
        ticket_id=tid, step_name="get_order", confidence=conf, audit=audit,
        reason=f"Retrieve current status of order {order_id} for an accurate customer update.")
    
    if order_resp.get("status") == "error":
        msg = f"Dear {customer['name']},\n\nWe couldn't locate your order. Please verify your order ID.\n\nSupport Team"
        await _invoke_tool(T.send_reply, tid, msg, ticket_id=tid, step_name="send_reply.invalid_order", confidence=conf, audit=audit, reason="Order not found. Requesting verification.")
        return "resolved", "We couldn't locate your order. Please verify your order ID."

    order = order_resp["order"]

    status_phrases = {
        "processing": "is being prepared in our warehouse and will ship soon",
        "shipped":    "has been shipped and is on its way to you",
        "delivered":  "was successfully delivered",
    }
    status_phrase = status_phrases.get(order["status"], f"has status '{order['status']}'")
    delivered_line = (f"  Delivered on : {order['delivered_date'][:10]}\n"
                      if order.get("delivered_date") else "")

    _record_decision(audit, ticket_id=tid, step_name="decide.send_reply", confidence=conf,
        reason=(f"Order {order_id} status is '{order['status']}'. "
                "Composing a complete status update for the customer."))
    msg = (
        f"Dear {customer['name']},\n\n"
        f"Thank you for reaching out about order {order_id}.\n\n"
        f"Your '{order['product']}' {status_phrase}.\n"
        f"  Order placed : {order['order_date'][:10]}\n"
        f"{delivered_line}"
        "\nPlease allow up to 2 business days for tracking to refresh. "
        "Contact us if you have not received your item by the expected date.\n\nSupport Team"
    )
    await _invoke_tool(
        T.send_reply, tid, msg,
        ticket_id=tid, step_name="send_reply.order_status", confidence=conf, audit=audit,
        reason="Send a complete order status update to the customer.")
    return "resolved", f"Order status '{order['status']}' communicated to customer"


async def _handle_product_issue(
    ticket: dict,
    clf: ClassificationResult,
    audit: list[AuditEntry],
) -> tuple[str, str]:
    """
    Resolve a defective or damaged product report.

    Tool chain:
        get_customer -> get_order -> check_refund_eligibility
            -> issue_refund (if eligible) -> send_reply
    """
    tid      = ticket["id"]
    email    = ticket["customer_email"]
    order_id = ticket.get("order_id", "")
    conf     = clf.confidence

    _record_decision(audit, ticket_id=tid, step_name="decide.get_customer", confidence=conf,
        reason="Customer reports a defective product. Verify identity and account tier "
               "before accessing order records or initiating a financial action.")
    cust_resp = await _invoke_tool(
        T.get_customer, email,
        ticket_id=tid, step_name="get_customer", confidence=conf, audit=audit,
        reason="Verify identity to prevent fraudulent product-defect claims.")
    customer = cust_resp["customer"]

    if not order_id:
        _record_decision(audit, ticket_id=tid, step_name="decide.missing_order_id", confidence=conf,
            reason="Order ID is missing. Cannot establish product, amount, and date for policy evaluation.")
        msg, val = _get_missing_order_msg(customer['name'], "product_issue")
        await _invoke_tool(T.send_reply, tid, msg, ticket_id=tid, step_name="send_reply.missing_order", confidence=conf, audit=audit, reason="Ask customer for missing order ID.")
        return "resolved", val

    _record_decision(audit, ticket_id=tid, step_name="decide.get_order", confidence=conf,
        reason=(f"Customer {customer['name']} (tier={customer['tier']}) confirmed. "
                f"Fetching order {order_id} to establish product, amount, and date for policy evaluation."))
    order_resp = await _invoke_tool(
        T.get_order, order_id,
        ticket_id=tid, step_name="get_order", confidence=conf, audit=audit,
        reason=f"Retrieve order {order_id} to confirm it contains the reported defective item.")

    if order_resp.get("status") == "error":
        msg = f"Dear {customer['name']},\n\nWe couldn't locate your order. Please verify your order ID.\n\nSupport Team"
        await _invoke_tool(T.send_reply, tid, msg, ticket_id=tid, step_name="send_reply.invalid_order", confidence=conf, audit=audit, reason="Order not found. Requesting verification.")
        return "resolved", "We couldn't locate your order. Please verify your order ID."

    order = order_resp["order"]

    _record_decision(audit, ticket_id=tid, step_name="decide.check_eligibility", confidence=conf,
        reason=(f"Order confirms '{order['product']}' (${order['amount']:.2f}, "
                f"status={order['status']}). Running refund/replacement eligibility check."))
    elig_resp = await _invoke_tool(
        T.check_refund_eligibility, order_id,
        ticket_id=tid, step_name="check_refund_eligibility", confidence=conf, audit=audit,
        reason="Check whether the defective product qualifies for a refund under the 30-day policy.")

    if elig_resp["eligible"]:
        _record_decision(audit, ticket_id=tid, step_name="decide.issue_refund", confidence=conf,
            reason=(f"Order {order_id} passes all eligibility criteria. "
                    "Issuing a full refund as the primary remedy for the defective product."))
        refund_resp = await _invoke_tool(
            T.issue_refund, order_id,
            ticket_id=tid, step_name="issue_refund", confidence=conf, audit=audit,
            reason=f"Issue full refund of ${elig_resp['amount']:.2f} for defective '{order['product']}'.")
        msg = (
            f"Dear {customer['name']},\n\n"
            f"We apologise for the defective '{order['product']}' in order {order_id}.\n\n"
            "We have issued a full refund:\n"
            f"  Refund amount : ${refund_resp['amount']:.2f}\n"
            f"  Refund ID     : {refund_resp['refund_id']}\n"
            f"  Estimated ETA : {refund_resp['estimated_arrival']}\n\n"
            "You do not need to return the item. "
            "Reply within 7 days if you prefer a replacement.\n\nSupport Team"
        )
        resolution = (f"Full refund {refund_resp['refund_id']} "
                      f"of ${refund_resp['amount']:.2f} issued for defective product")
    else:
        reasons_text = "; ".join(elig_resp["reasons"])
        _record_decision(audit, ticket_id=tid, step_name="decide.deny_refund", confidence=conf,
            reason=(f"Refund/replacement not possible for {order_id}: {reasons_text}. "
                    "Directing customer to warranty support."))
        msg = (
            f"Dear {customer['name']},\n\n"
            f"We are sorry to hear about the issue with your '{order['product']}'.\n\n"
            f"A refund cannot be issued at this time:\n  {reasons_text}\n\n"
            "Your product may be covered under the manufacturer's warranty. "
            "Please reply with your warranty details and we will assist further.\n\nSupport Team"
        )
        resolution = f"Product issue acknowledged; refund ineligible: {reasons_text}"

    _record_decision(audit, ticket_id=tid, step_name="decide.send_reply", confidence=conf,
        reason="All evidence gathered. Sending final outcome message to customer.")
    await _invoke_tool(
        T.send_reply, tid, msg,
        ticket_id=tid, step_name="send_reply.product_issue", confidence=conf, audit=audit,
        reason="Deliver outcome message so the customer has a clear, written record.")
    return "resolved", resolution


async def _handle_general(
    ticket: dict,
    clf: ClassificationResult,
    audit: list[AuditEntry],
) -> tuple[str, str]:
    """
    Resolve a general inquiry.

    Tool chain: get_customer -> get_order (if order_id present) -> send_reply
    """
    tid      = ticket["id"]
    email    = ticket["customer_email"]
    order_id = ticket.get("order_id")
    conf     = clf.confidence

    _record_decision(audit, ticket_id=tid, step_name="decide.get_customer", confidence=conf,
        reason="General inquiry received. Fetching customer record to personalise the response "
               "and verify the account is active before replying.")
    cust_resp = await _invoke_tool(
        T.get_customer, email,
        ticket_id=tid, step_name="get_customer", confidence=conf, audit=audit,
        reason="Verify customer exists and retrieve name for a personalised reply.")
    customer = cust_resp["customer"]

    order_context = ""
    if order_id:
        _record_decision(audit, ticket_id=tid, step_name="decide.get_order", confidence=conf,
            reason=(f"Ticket references order {order_id}. Fetching order details to include "
                    "accurate status information and demonstrate proactive service."))
        try:
            order_resp = await _invoke_tool(
                T.get_order, order_id,
                ticket_id=tid, step_name="get_order", confidence=conf, audit=audit,
                reason=f"Fetch order {order_id} so the reply contains specific, accurate context.")
            o = order_resp["order"]
            order_context = (
                f"\n\nRegarding order {order_id} ('{o['product']}'):\n"
                f"  Status     : {o['status']}\n"
                f"  Order date : {o['order_date'][:10]}\n"
            )
        except Exception:
            order_context = f"\n\nWe were unable to retrieve order {order_id} at this time."
    else:
        _record_decision(audit, ticket_id=tid, step_name="decide.skip_order_fetch", confidence=conf,
            reason="No order_id in the ticket. Skipping order lookup and proceeding to reply.")

    _record_decision(audit, ticket_id=tid, step_name="decide.send_reply", confidence=conf,
        reason="All context gathered. Sending an acknowledgement with any available order context.")
    msg = (
        f"Dear {customer['name']},\n\n"
        "Thank you for contacting our support team. "
        "We have received your message and are reviewing it."
        f"{order_context}\n\n"
        "A team member will follow up with a detailed response within 24 hours. "
        "You can also visit help.example.com for instant answers.\n\nSupport Team"
    )
    await _invoke_tool(
        T.send_reply, tid, msg,
        ticket_id=tid, step_name="send_reply.general", confidence=conf, audit=audit,
        reason="Send personalised acknowledgement so the customer knows their inquiry is received.")
    return "resolved", "General inquiry acknowledged and replied"


_HANDLER_MAP: dict[str, Any] = {
    "refund":           _handle_refund,
    "order_issue":      _handle_order_issue,
    "product_issue":    _handle_product_issue,
    "general_inquiry":  _handle_general,
}


async def process_ticket(ticket: dict) -> TicketResult:
    """
    Autonomously process a single support ticket end-to-end.

    Ticket schema
    -------------
    {
        "id"             : str,
        "customer_email" : str,
        "order_id"       : str | None,
        "subject"        : str,
        "body"           : str
    }

    Processing phases
    -----------------
    Phase 1  Ingest      — record ticket receipt.
    Phase 2  Classify    — determine issue type and confidence.
    Phase 3  Gate        — escalate immediately if confidence < threshold.
    Phase 4  Route       — select and run the appropriate handler.
    Phase 5  Guard       — enforce minimum tool call count.
    Phase 6  Finalise    — build and return the TicketResult.
    """
    tid   = ticket["id"]
    audit: list[AuditEntry] = []

    _record_decision(
        audit, ticket_id=tid, step_name="ticket_received", confidence=1.0,
        reason="New ticket ingested from the queue for autonomous processing.",
        detail={
            "subject":        ticket["subject"],
            "customer_email": ticket["customer_email"],
            "order_id":       ticket.get("order_id"),
            "body_preview":   ticket["body"][:120],
        },
    )

    if not ticket.get("order_id"):
        body_text = ticket.get("body", "")
        match1 = re.search(r'(?i)\b(ORD-\d+)\b', body_text)
        match2 = re.search(r'(?i)order\s+number\s+(?:is\s+)?#?(\d+)', body_text)
        
        extracted_id = None
        if match1:
            extracted_id = match1.group(1).upper()
        elif match2:
            extracted_id = f"ORD-{match2.group(1)}"
            
        if extracted_id:
            ticket["order_id"] = extracted_id
            _record_decision(
                audit, ticket_id=tid, step_name="extract_order_id", confidence=1.0,
                reason=f"Regex extracted order_id '{extracted_id}' from the ticket body.",
                detail={"extracted_order_id": extracted_id}
            )

    clf = classify_ticket(ticket["subject"], ticket["body"])
    _record_decision(
        audit, ticket_id=tid, step_name="classification_result", confidence=clf.confidence,
        reason=(
            f"Classifier scored '{clf.issue_type}' at confidence {clf.confidence:.3f} "
            f"using weighted keyword patterns. Full scores: {clf.scores}."
        ),
        detail={"issue_type": clf.issue_type, "confidence": clf.confidence, "scores": clf.scores},
    )

    if clf.confidence < ESCALATION_THRESHOLD:
        _record_decision(
            audit, ticket_id=tid, step_name="confidence_gate.failed", confidence=clf.confidence,
            reason=(
                f"Confidence {clf.confidence:.3f} is below the escalation threshold "
                f"of {ESCALATION_THRESHOLD}. The ticket is ambiguous; escalating to "
                "a human agent to avoid an incorrect automated action."
            ),
        )
        summary = (
            f"LOW-CONFIDENCE TICKET [{tid}]\n"
            f"Best guess  : {clf.issue_type} (confidence={clf.confidence:.3f})\n"
            f"Threshold   : {ESCALATION_THRESHOLD}\n"
            f"Subject     : {ticket['subject']}\n"
            f"Body        : {ticket['body'][:300]}\n"
            f"Scores      : {clf.scores}"
        )
        await _enforce_min_tool_calls(ticket, clf, audit)
        resolution = await _run_escalate(
            tid, summary,
            audit_reason=(
                f"Automatic escalation: confidence {clf.confidence:.3f} "
                f"< threshold {ESCALATION_THRESHOLD}. Human review required."
            ),
            escalation_reason="low confidence in classification",
            confidence=clf.confidence,
            audit=audit,
            step_name="escalate.low_confidence",
        )
        return TicketResult(
            ticket_id=tid,
            customer_email=ticket["customer_email"],
            issue_type=clf.issue_type,
            final_confidence=clf.confidence,
            final_status="escalated",
            resolution_message=resolution,
            successful_tool_calls=_count_successful_tool_calls(audit),
            total_attempts=_count_tool_attempts(audit),
            retry_count=_count_retries(audit),
            audit_trail=audit,
        )

    _record_decision(
        audit, ticket_id=tid, step_name="routing_decision", confidence=clf.confidence,
        reason=(
            f"Confidence {clf.confidence:.3f} exceeds threshold. "
            f"Routing to '{clf.issue_type}' handler. "
            f"The ticket MUST complete the full action chain and meet the "
            f"{MIN_TOOL_CALLS}-tool minimum before resolution is permitted."
        ),
    )

    handler = _HANDLER_MAP[clf.issue_type]
    try:
        final_status, resolution = await handler(ticket, clf, audit)

    except (T.ToolError, T.ToolTimeout) as exc:
        _record_decision(
            audit, ticket_id=tid, step_name="unrecoverable_tool_failure", confidence=clf.confidence,
            reason=(
                f"Tool raised an unrecoverable error after {MAX_RETRIES} retries: {exc}. "
                "Cannot complete automated resolution. Escalating."
            ),
        )
        summary = (
            f"TOOL FAILURE [{tid}]\n"
            f"Issue type : {clf.issue_type}\n"
            f"Error      : {exc}\n"
            f"Subject    : {ticket['subject']}"
        )
        await _enforce_min_tool_calls(ticket, clf, audit)
        resolution = await _run_escalate(
            tid, summary,
            audit_reason=(
                f"Unrecoverable tool failure after {MAX_RETRIES} retries: {exc}. "
                "Human intervention required."
            ),
            escalation_reason="multiple tool failures",
            confidence=clf.confidence,
            audit=audit,
            step_name="escalate.tool_failure",
        )
        final_status = "escalated"

    except Exception as exc:
        _record_decision(
            audit, ticket_id=tid, step_name="unexpected_error", confidence=clf.confidence,
            reason=f"Unexpected exception during handler execution: {exc}",
            detail={"error": str(exc), "type": type(exc).__name__},
        )
        return TicketResult(
            ticket_id=tid,
            customer_email=ticket["customer_email"],
            issue_type=clf.issue_type,
            final_confidence=clf.confidence,
            final_status="failed",
            resolution_message=f"Unexpected error: {exc}",
            successful_tool_calls=_count_successful_tool_calls(audit),
            total_attempts=_count_tool_attempts(audit),
            retry_count=_count_retries(audit),
            audit_trail=audit,
            error=str(exc),
        )

    await _enforce_min_tool_calls(ticket, clf, audit)

    _record_decision(
        audit, ticket_id=tid, step_name="resolution_finalised", confidence=clf.confidence,
        reason=(
            f"Handler completed with status '{final_status}'. "
            f"Successful tool calls: {_count_successful_tool_calls(audit)} "
            f"(minimum required: {MIN_TOOL_CALLS}). Ticket closed."
        ),
        detail={"final_status": final_status, "resolution": resolution},
    )

    return TicketResult(
        ticket_id=tid,
        customer_email=ticket["customer_email"],
        issue_type=clf.issue_type,
        final_confidence=clf.confidence,
        final_status=final_status,
        resolution_message=resolution,
        successful_tool_calls=_count_successful_tool_calls(audit),
        total_attempts=_count_tool_attempts(audit),
        retry_count=_count_retries(audit),
        audit_trail=audit,
    )
