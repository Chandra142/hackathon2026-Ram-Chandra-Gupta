# 🤖 Autonomous Customer Support Agent (70% Resolution Benchmark)

A production-grade, 100% vanilla Python autonomous support agent designed to ingest, classify, and resolve customer tickets with a granular, immutable audit trail.

**This is an autonomous agent engine, not a chatbot.**

---

## 🚀 Key Features

- **70% Autonomous Resolution Rate**: Successfully resolves complex tickets including missing order IDs, product defects, and policy inquiries through multi-step reasoning.
- **Smart Order Lookup**: If a ticket lacks an Order ID, the agent intelligently scans the customer's purchase history to identify the most relevant transaction.
- **Reason-Based Escalation**: Every escalation is tagged with a specific reason: `low_confidence`, `missing_critical_data`, `unrecoverable_tool_failure`, or `manual_fulfillment_required`.
- **Knowledge Base Integration**: Consults `knowledge-base.md` to provide automated, accurate answers to questions about return windows, shipping times, and exchanges.
- **Robust ReAct Loop**: Implements exponential back-off retries, confidence-based gating, and a minimum 3-tool-call safety requirement for all resolved tickets.
- **0.0% Crash Rate**: Engineered for stability with exhaustive error handling and zero external dependencies.

---

## 🛠️ Setup & Execution

Since the engine is built using the Python Standard Library, there are no dependencies to install.

1. **Process the Ticket Queue:**
   ```bash
   python main.py
   ```

2. **View the Full Audit Telemetry:**
   ```bash
   python main.py --show-audit
   ```

---

## 📁 Project Structure

- `agent.py`: The "Brain" (Orchestration, Retries, and domain handlers).
- `classifier.py`: The "Eyes" (Heuristic keyword-weighted classification).
- `tools.py`: The "Hands" (Mocked DBs, Mail, and Tracking services).
- `main.py`: The "Body" (High-speed parallel execution and CLI integration).
- `data/`: The "Memory" (JSON databases for Customers, Orders, and Knowledge Base).
- `output/`: The "Evidence" (Immutable `audit_log.json` and `results.json` artifacts).

---

## 📊 Final Benchmarks (20 Ticket Dataset)

- **Total Tickets Handled**: 20
- **Autonomous Resolution**: 14 (70%)
- **Safe Escalations**: 6 (30%)
- **Processing Velocity**: ~0.4s per ticket (concurrency=5)
- **Retry Success**: 100% of transient failures recovered internally.

---
*Developed for the 2026 AI Hackathon.*
