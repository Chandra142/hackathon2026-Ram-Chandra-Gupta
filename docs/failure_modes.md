# Failure Modes — Autonomous Customer Support Agent

This document describes how the agent detects, handles, and recovers from failure conditions. Each scenario is grounded in actual system behaviour implemented in `agent.py`.

---

## Failure Mode 1 — Tool Timeout or Exception

### What Goes Wrong

An external tool (`get_customer`, `get_order`, `check_refund_eligibility`, `issue_refund`, `send_reply`) raises a `ToolTimeout` or `ToolError` during execution. This can happen due to simulated network latency spikes, service unavailability, or data integrity errors. Failure rates per tool range from 5% to 12%.

### How the System Detects It

`_invoke_tool()` wraps every tool call in a `try/except` block catching both `ToolTimeout` and `ToolError`. The moment either is raised, the failure is captured along with the attempt number, elapsed duration, and the error message.

```
Attempt 1 fails → status: "retry" logged → wait 0.4s
Attempt 2 fails → status: "retry" logged → wait 0.8s
Attempt 3 fails → status: "error" logged → exception re-raised
```

### How the System Responds

Each failed attempt writes an `AuditEntry` with `status = "retry"`. Each back-off wait writes a `decision`-type entry explaining the error, the wait duration, and remaining attempts. After all three attempts fail, the exception propagates to `process_ticket()`, which calls `_run_escalate()` with a structured summary including the ticket ID, issue type, and exact error message.

### Why This Approach Is Reliable

- No silent failures — every attempt is recorded regardless of outcome.
- Exponential back-off (0.4 s, 0.8 s) gives transient services time to recover without aggressive hammering.
- Escalation is unconditional — if automation cannot complete, a human always receives the ticket with full context.
- The audit trail survives the failure, so engineers can replay exactly what was attempted.

---

## Failure Mode 2 — Missing or Invalid Data

### What Goes Wrong

A tool requests a record that does not exist in the database — for example, a ticket from an unrecognised customer email, or a request inquiring about an invalid order ID (e.g. `ORD-9999`).

### How the System Detects It

Instead of blindly assuming data exists or crashing with a `KeyError`, the tools (`get_customer()` and `get_order()`) proactively validate against the backend datastore arrays. When a miss occurs, the tools intercept the failure before it can break the application execution flow.

### How the System Responds

The system returns structured fallback objects rather than raising fatal exceptions:
1. **Unrecognised Customers**: `get_customer()` logs the error and returns a generic fallback profile (`{"name": "Unknown User", "tier": "standard"}`).
2. **Invalid Orders**: `get_order()` logs the error and returns a safe, dummy order payload with a `"not_found"` status and zeroed values (e.g. `"amount": 0.0`).

This allows the main agent loop to continue without crashing. The agent evaluates the fallback data (e.g., denying a refund request because a `"not_found"` order is not in `"delivered"` status) and resolves the ticket by communicating the missing identifier to the user.

### Why This Approach Is Reliable

- **No Application Crashes**: Safe JSON dictionaries prevent downstream `TypeError` or `KeyError` crashes inside the agent reasoning loop.
- **Automated Deflection**: The agent replies to the customer to deny invalid requests rather than silently dropping them or burdening the human escalation queue with faulty data.
- **Robustness**: The action chain processes normally to closure.

## Failure Mode 3 — Low Confidence Classification

### What Goes Wrong

The classifier cannot assign a clear issue type because the ticket body is too short, ambiguous, or contains no recognisable keywords. The total weighted keyword score falls below the minimum threshold, or the winning category does not clearly dominate the others.

**Example:** Ticket with `subject = "hmm"` and `body = "hi"` produces `confidence = 0.35`.

### How the System Detects It

After `classify_ticket()` returns, `process_ticket()` immediately evaluates:

```
if clf.confidence < ESCALATION_THRESHOLD (0.6):
    → confidence_gate.failed
```

This check runs **before** any handler is selected and **before** any tool is invoked. The system does not attempt to guess the intent or run a default handler.

### How the System Responds

1. A `confidence_gate.failed` decision entry is logged with the confidence score, threshold, and reason.
2. `_run_escalate()` is called immediately with a structured summary containing the best-guess issue type, the raw score, all category scores, the full subject, and up to 300 characters of the body.
3. `final_status = "escalated"` is set. No tool chain runs. No customer reply is sent.

### Why This Approach Is Reliable

- An incorrect automated action (wrong refund, wrong status reply) is far more damaging than a short escalation delay. Routing uncertain tickets to humans is the conservative and correct choice.
- The escalation summary includes the full classifier score breakdown, giving the human agent insight into why the ticket was ambiguous.
- The threshold (0.6) is a named constant (`ESCALATION_THRESHOLD`) — it can be tuned without touching handler logic.

---

## Failure Mode 4 — Partial Execution / Interrupted Flow *(Bonus)*

### What Goes Wrong

The action chain begins successfully — for example, `get_customer` and `get_order` succeed — but a later tool fails all three retry attempts. The ticket cannot be fully resolved because the critical step (e.g. `issue_refund` or `send_reply`) could not complete.

```
get_customer()          → success  (attempt 1)
get_order()             → success  (attempt 1)
check_refund_eligibility() → success  (attempt 1)
issue_refund()          → retry    (attempt 1) → retry (attempt 2) → error (attempt 3)
```

### How the System Detects It

`_invoke_tool()` raises the last exception after exhausting all attempts. This propagates up through the handler function to the `except (T.ToolError, T.ToolTimeout)` block in `process_ticket()`. At this point the audit trail already contains all successful steps and all retry attempts.

### How the System Responds

1. An `unrecoverable_tool_failure` decision entry is written, referencing the failed tool and the exception.
2. `_run_escalate()` is called with a summary identifying the ticket, the issue type, the failed tool, and the error.
3. `final_status = "escalated"` is set on the `TicketResult`.
4. The complete audit trail — including all steps that **did** succeed — is preserved in `audit_log.json`.

The human agent receives the ticket with full context: what was verified (customer identity, order details, eligibility), what was attempted, and exactly where the failure occurred.

### Why This Approach Is Reliable

- Partial state is never silently discarded. Every successful step remains in the audit trail and is written to `audit_log.json` at the end of the run regardless of outcome.
- The system never leaves a ticket in an ambiguous state — it always resolves to one of three terminal statuses: `resolved`, `escalated`, or `failed`.
- Because refunds are idempotent-guarded in `tools.py` (duplicate refund raises `ToolError`), a partial retry cannot accidentally issue the same refund twice.
- The human agent can re-run eligible steps manually without risk of duplicate actions, because the audit trail clearly shows which steps completed.

---

## Summary Table

| # | Failure Mode | Detection Point | Agent Response | Terminal Status |
|---|---|---|---|---|
| 1 | Tool timeout / exception | `_invoke_tool()` — per attempt | Retry ×2 → escalate with summary | `escalated` |
| 2 | Missing / invalid record | `get_customer()` / `get_order()` dictionary checks | Return safe fallback objects → reason naturally | `resolved` (denied/explained) |
| 3 | Low confidence classification | `process_ticket()` — confidence gate | Skip action chain → escalate immediately | `escalated` |
| 4 | Partial execution interrupted | `process_ticket()` — handler exception | Preserve audit trail → escalate with context | `escalated` |

All failure modes share a single invariant: **the agent never silently drops a ticket**. Every failure is logged, justified, and handed to a human with full context.
