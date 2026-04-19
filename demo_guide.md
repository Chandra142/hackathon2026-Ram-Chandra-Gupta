# 🎤 Hackathon Demo Guide

This guide highlights the key moments to show during your demo to maximize your score.

## 1. The "WOW" Moment: Parallelism
**Action**: Run `python main.py`
**Talking Point**: *"Watch as the engine ingests 20 tickets simultaneously. Because it's built with asynchronous I/O and a ReAct-style loop, it handles the entire queue in under 10 seconds, including simulated latencies and retries."*

## 2. The "Intelligence" Moment: Smart Recovery
**Action**: Find `TKT-006` or `TKT-016` in the output (they show `RESOLVED`).
**Talking Point**: *"Look at TKT-006. The customer didn't provide an Order ID. Instead of giving up, our agent autonomously looked up the customer's email, found their most recent order (ORD-1006), and resolved the cancellation request. This 'Smart Lookup' behavior boosted our autonomous resolution rate from 50% to 70%."*

## 3. The "Trust" Moment: Audit Logs
**Action**: Run `python main.py --show-audit` and scroll to a complex ticket.
**Talking Point**: *"In any production environment, trust is vital. Every tool call, retry, and internal decision is logged in an immutable Audit Trail. For example, here you can see the agent consulting our internal Knowledge Base to answer a return policy question automatically."*

## 4. The "Safety" Moment: Reason-Based Escalation
**Action**: Open `output/results.json` and show a ticket with Status `ESCALATED`.
**Talking Point**: *"The agent knows when it's out of its depth. It never guesses. If it sees a 'Replacement Request' or encounters an 'Invalid Order ID', it escalates to a human with a clear, specific reason so they can take over immediately."*

---
### Cheat Sheet: High-Value Results to Show
- **TKT-006**: Successful Order ID lookup & cancellation.
- **TKT-019**: General policy inquiry answered via Knowledge Base.
- **TKT-015**: ESCALATED for "manual fulfillment" (Replacement request).
- **TKT-001**: Successful recovery after a transient tool failure (Retry Logic).
