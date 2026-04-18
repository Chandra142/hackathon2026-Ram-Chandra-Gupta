"""
main.py

Entry point for the autonomous customer support agent.

Usage
-----
    python -X utf8 main.py                        # use tickets.json in same dir
    python -X utf8 main.py path/to/tickets.json   # custom ticket file
    python -X utf8 main.py --concurrency 10       # set parallel worker limit
    python -X utf8 main.py --show-audit           # print per-ticket audit table

Output files (written to --output-dir, default: ./output/)
-----------------------------------------------------------
    audit_log.json   full step-by-step log for every ticket
    results.json     final status summary for every ticket
"""

import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    _rich = True
    console = Console()
except ImportError:
    _rich = False
    class _FakeConsole:
        def print(self, *a, **kw): print(*a)
    console = _FakeConsole()  # type: ignore[assignment]

from agent import process_ticket, TicketResult, AuditEntry

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

_STATUS_RICH  = {"resolved": "green", "escalated": "yellow", "failed": "red"}
_STATUS_ANSI  = {"resolved": GREEN,   "escalated": YELLOW,   "failed": RED}

def _print_banner() -> None:
    if _rich:
        console.print(Panel.fit(
            "[bold cyan]Autonomous Customer Support Agent[/]\n"
            "[dim]Classify | Reason | Act | Retry (x2) | Escalate | Audit[/]",
            border_style="cyan",
        ))
    else:
        print(f"\n{BOLD}{'='*62}")
        print("  Autonomous Customer Support Agent")
        print(f"  Classify | Reason | Act | Retry (x2) | Escalate | Audit")
        print(f"{'='*62}{RESET}\n")


def _print_ticket_audit(result: TicketResult) -> None:
    """Print a per-ticket step-by-step audit table to the console."""
    header = (f"\nAudit trail — {result.ticket_id}  "
              f"[{result.issue_type}, conf={result.final_confidence:.3f}]")
    if _rich:
        console.print(f"\n[bold underline]{header.strip()}[/]")
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold dim")
        table.add_column("#",         width=3,  justify="right")
        table.add_column("step_name", width=35, no_wrap=True)
        table.add_column("tool_name", width=24, no_wrap=True)
        table.add_column("status",    width=9,  no_wrap=True)
        table.add_column("attempt",   width=7,  justify="right")
        table.add_column("conf",      width=5,  justify="right")
        table.add_column("ms",        width=6,  justify="right")
        table.add_column("reason",    width=52)
        _sc = {"success": "green", "retry": "yellow", "error": "red", "decision": "cyan"}
        for i, e in enumerate(result.audit_trail, 1):
            sc = _sc.get(e.status, "white")
            table.add_row(
                str(i),
                e.step_name,
                e.tool_name or "[dim]-[/]",
                f"[{sc}]{e.status}[/]",
                str(e.attempt),
                f"{e.confidence:.2f}",
                f"{e.duration_ms:.0f}" if e.duration_ms else "",
                e.reason[:52],
            )
        console.print(table)
    else:
        print(header)
        for i, e in enumerate(result.audit_trail, 1):
            ms = f"{e.duration_ms:.0f}ms" if e.duration_ms else ""
            tool = e.tool_name or "-"
            print(f"  {i:>2}. [{e.status:<8}] {e.step_name:<35} {tool:<24} "
                  f"att={e.attempt} {ms:<6} {e.reason[:55]}")


def _print_results_table(results: list[TicketResult]) -> None:
    if _rich:
        table = Table(title="Ticket Processing Results", box=box.ROUNDED,
                      show_lines=True, header_style="bold magenta")
        table.add_column("Ticket",     style="bold",  no_wrap=True)
        table.add_column("Issue Type", style="cyan",  no_wrap=True)
        table.add_column("Confidence", justify="right")
        table.add_column("OK Calls",   justify="right")
        table.add_column("Retries",    justify="right")
        table.add_column("Attempts",   justify="right")
        table.add_column("Status",     no_wrap=True)
        table.add_column("Resolution", max_width=40)
        for r in results:
            colour = _STATUS_RICH.get(r.final_status, "white")
            retry_style = "[yellow]" if r.retry_count > 0 else "[dim]"
            table.add_row(
                r.ticket_id,
                r.issue_type,
                f"{r.final_confidence:.3f}",
                str(r.successful_tool_calls),
                f"{retry_style}{r.retry_count}[/]",
                str(r.total_attempts),
                f"[{colour}]{r.final_status.upper()}[/]",
                r.resolution_message,
            )
        console.print(table)
    else:
        hdr = (f"{'Ticket':<10} {'Type':<16} {'Conf':>6} {'OK':>4} "
               f"{'Ret':>4} {'Att':>4} {'Status':<11} Resolution")
        print(f"\n{BOLD}{hdr}{RESET}")
        print("-" * 100)
        for r in results:
            ansi = _STATUS_ANSI.get(r.final_status, RESET)
            print(f"{r.ticket_id:<10} {r.issue_type:<16} {r.final_confidence:>6.3f} "
                  f"{r.successful_tool_calls:>4} {r.retry_count:>4} {r.total_attempts:>4} "
                  f"{ansi}{r.final_status.upper():<11}{RESET} {r.resolution_message[:40]}")


def _print_run_summary(results: list[TicketResult], elapsed: float) -> None:
    resolved  = sum(1 for r in results if r.final_status == "resolved")
    escalated = sum(1 for r in results if r.final_status == "escalated")
    failed    = sum(1 for r in results if r.final_status == "failed")
    ok_calls  = sum(r.successful_tool_calls for r in results)
    retries   = sum(r.retry_count for r in results)
    attempts  = sum(r.total_attempts for r in results)
    low_conf  = sum(1 for r in results
                    if r.final_status == "escalated" and r.final_confidence < 0.6)
    if _rich:
        txt = (
            f"[bold]Tickets:[/] {len(results)}   "
            f"[green]Resolved:[/] {resolved}   "
            f"[yellow]Escalated:[/] {escalated}   "
            f"[red]Failed:[/] {failed}\n"
            f"[bold]Successful tool calls:[/] {ok_calls}   "
            f"[bold]Total attempts:[/] {attempts}   "
            f"[yellow]Retries:[/] {retries}\n"
            f"[dim]Low-confidence escalations: {low_conf}   "
            f"Wall time: {elapsed:.2f}s[/]"
        )
        console.print(Panel(txt, title="Run Summary", border_style="green"))
    else:
        print(f"\n{BOLD}Run Summary{RESET}")
        print(f"  Tickets   : {len(results)}  Resolved: {resolved}  "
              f"Escalated: {escalated}  Failed: {failed}")
        print(f"  OK calls  : {ok_calls}  Total attempts: {attempts}  Retries: {retries}")
        print(f"  Low-conf escalations: {low_conf}  Wall time: {elapsed:.2f}s")


def _entry_to_dict(e: AuditEntry) -> dict:
    return {
        "ticket_id":     e.ticket_id,
        "timestamp":     e.timestamp,
        "step_name":     e.step_name,
        "tool_name":     e.tool_name,
        "input":         e.input,
        "output":        e.output if isinstance(e.output, (dict, list, type(None))) else str(e.output),
        "reason":        e.reason,
        "confidence":    e.confidence,
        "status":        e.status,
        "attempt":       e.attempt,
        "duration_ms":   e.duration_ms,
        "error_message": e.error_message,
    }


def _result_to_dict(r: TicketResult) -> dict:
    return {
        "ticket_id":            r.ticket_id,
        "customer_email":       r.customer_email,
        "issue_type":           r.issue_type,
        "final_confidence":     r.final_confidence,
        "final_status":         r.final_status,
        "resolution_message":   r.resolution_message,
        "successful_tool_calls":r.successful_tool_calls,
        "total_attempts":       r.total_attempts,
        "retry_count":          r.retry_count,
        "error":                r.error,
    }


async def _process_bounded(ticket: dict, semaphore: asyncio.Semaphore) -> TicketResult:
    async with semaphore:
        if _rich:
            console.print(f"  [dim]>>[/] [bold]{ticket['id']}[/]  "
                          f"[italic dim]{ticket['subject'][:70]}[/]")
        else:
            print(f"  >> {ticket['id']}  {ticket['subject'][:70]}")

        result = await process_ticket(ticket)

        colour = _STATUS_RICH.get(result.final_status, "white")
        suffix = (f"  ({result.retry_count} retr{'y' if result.retry_count == 1 else 'ies'})"
                  if result.retry_count else "")
        if _rich:
            console.print(f"  [dim]OK[/] [bold]{result.ticket_id}[/] -> "
                          f"[{colour}]{result.final_status.upper()}[/]  "
                          f"[dim]{result.successful_tool_calls} tool calls{suffix}[/]")
        else:
            ansi = _STATUS_ANSI.get(result.final_status, RESET)
            print(f"  OK {result.ticket_id} -> "
                  f"{ansi}{result.final_status.upper()}{RESET}  "
                  f"{result.successful_tool_calls} tool calls{suffix}")
        return result


async def run(ticket_file: str, concurrency: int, output_dir: str, show_audit: bool) -> None:
    _print_banner()

    path = Path(ticket_file)
    if not path.exists():
        console.print(f"[red]Error:[/] File not found: {ticket_file}")
        sys.exit(1)

    with open(path, encoding="utf-8") as fh:
        tickets: list[dict] = json.load(fh)

    for t in tickets:
        if "id" not in t and "ticket_id" in t:
            t["id"] = t["ticket_id"]

    config_line = (f"Loaded {len(tickets)} ticket(s)  |  concurrency={concurrency}  "
                   f"min_tool_calls=3  max_retries=2  escalation_threshold=0.6")
    if _rich:
        console.print(f"\n[bold]{config_line}[/]\n")
    else:
        print(f"\n{config_line}\n")

    semaphore = asyncio.Semaphore(concurrency)
    t0 = time.monotonic()
    results: list[TicketResult] = await asyncio.gather(
        *[_process_bounded(t, semaphore) for t in tickets]
    )
    elapsed = time.monotonic() - t0

    msg = f"\nAll {len(results)} ticket(s) processed in {elapsed:.2f}s\n"
    console.print(f"[dim]{msg.strip()}[/]" if _rich else msg)

    if show_audit:
        for r in results:
            _print_ticket_audit(r)

    _print_results_table(results)
    _print_run_summary(results, elapsed)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    audit_path = out / "audit_log.json"
    audit_payload = {
        "generated_at":   datetime.now().isoformat(),
        "total_tickets":  len(results),
        "elapsed_seconds":round(elapsed, 3),
        "agent_config": {
            "min_tool_calls":        3,
            "max_retries_per_tool":  2,
            "escalation_threshold":  0.6,
            "parallel_processing":   True,
        },
        "tickets": {
            r.ticket_id: [_entry_to_dict(e) for e in r.audit_trail]
            for r in results
        },
    }
    with open(audit_path, "w", encoding="utf-8") as fh:
        json.dump(audit_payload, fh, indent=2, default=str)

    results_path = out / "results.json"
    results_payload = {
        "generated_at":   datetime.now().isoformat(),
        "elapsed_seconds":round(elapsed, 3),
        "summary": {
            "total":                  len(results),
            "resolved":               sum(1 for r in results if r.final_status == "resolved"),
            "escalated":              sum(1 for r in results if r.final_status == "escalated"),
            "failed":                 sum(1 for r in results if r.final_status == "failed"),
            "successful_tool_calls":  sum(r.successful_tool_calls for r in results),
            "total_attempts":         sum(r.total_attempts for r in results),
            "total_retries":          sum(r.retry_count for r in results),
        },
        "tickets": [_result_to_dict(r) for r in results],
    }
    with open(results_path, "w", encoding="utf-8") as fh:
        json.dump(results_payload, fh, indent=2, default=str)

    if _rich:
        console.print(f"\n[bold green]Output files written:[/]")
        console.print(f"   [cyan]{audit_path.resolve()}[/]")
        console.print(f"   [cyan]{results_path.resolve()}[/]")
    else:
        print(f"\nOutput files written:")
        print(f"  {audit_path.resolve()}")
        print(f"  {results_path.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Customer Support Agent",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("ticket_file", nargs="?",
                        default=str(Path(__file__).parent / "data" / "tickets.json"),
                        help="Path to the JSON ticket file.")
    parser.add_argument("--concurrency", "-c", type=int, default=5,
                        help="Maximum concurrent tickets.")
    parser.add_argument("--output-dir", "-o",
                        default=str(Path(__file__).parent / "output"),
                        help="Directory for output JSON files.")
    parser.add_argument("--show-audit", action="store_true",
                        help="Print per-ticket audit trail to the console.")
    args = parser.parse_args()
    asyncio.run(run(args.ticket_file, args.concurrency, args.output_dir, args.show_audit))


if __name__ == "__main__":
    main()
