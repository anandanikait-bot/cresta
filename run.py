from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agent.models import jsonable
from agent.rules import RuleConfigError, load_rules
from agent.sources import SourceLoadError, load_sources
from agent.state import snapshots_for_questions
from agent.workflow import BillingWorkflow


ROOT = Path(__file__).resolve().parent


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Offline, evidence-grounded NimbusCare billing workflow demo")
    result.add_argument("--policy", type=Path, default=ROOT / "data" / "Nimbus Wireless — Billing Dispute & Refund Policy.md")
    result.add_argument("--kb", type=Path, default=ROOT / "data" / "NimbusCare Knowledge Base — Billing & Credits Excerpt.md")
    result.add_argument("--transcript", type=Path, default=ROOT / "data" / "NimbusCare Chat Transcript — Dana Whitfield, October 5, 2026.txt")
    result.add_argument("--rules", type=Path, default=ROOT / "config" / "nimbus_policy_rules.json")
    result.add_argument("--question", help="Answer one general customer question using supplied sources")
    result.add_argument("--use-demo-context", action="store_true", help="Use the demo transcript facts with --question")
    result.add_argument("--json", dest="json_path", type=Path, help="Write the full trace as JSON to this path")
    return result


def _run_metadata(sources: Any, workflow: BillingWorkflow) -> dict[str, Any]:
    return {
        "source_documents": sources.documents,
        "warnings": list(sources.warnings),
        "source_conflicts": list(workflow.conflicts),
        "policy_sections_parsed": len(sources.rules),
        "kb_articles_parsed": len(sources.articles),
        "language_mode": "offline deterministic",
        "external_services_used": False,
    }


def _render_trace(trace: Any, index: int | None = None) -> str:
    header = f"Turn {index}: {trace.timestamp} — {trace.question}" if index is not None else f"Question: {trace.question}"
    rows = ["=" * min(100, max(20, len(header))), header, f"Intent: {trace.intent} | Language: {trace.language_mode}"]
    if trace.facts_snapshot:
        rows.append("Facts visible at this turn:")
        rows.append(json.dumps(trace.facts_snapshot, ensure_ascii=False, indent=2))
    rows.append("Retrieved evidence:")
    if trace.retrieved:
        rows.extend(f"  - {item.passage_id} (score {item.score:.2f}): {item.excerpt}" for item in trace.retrieved)
    else:
        rows.append("  - no matching passage")
    decision = trace.decision
    rows.append(f"Decision: {decision.status}; selected rule: {decision.selected_rule_id or 'none'}")
    if decision.reason:
        rows.append(f"Reason: {decision.reason}")
    if decision.calculations:
        rows.append("Calculation and checks: " + json.dumps(jsonable(decision.calculations), ensure_ascii=False, sort_keys=True))
    if decision.approval_reasons:
        rows.append("Tier 2 reasons: " + ", ".join(decision.approval_reasons))
    if decision.unresolved_checks:
        rows.append("Unknown/pending checks: " + ", ".join(decision.unresolved_checks))
    if decision.exclusions:
        rows.append("Excluded controls: " + "; ".join(decision.exclusions))
    rows.append("Claim grounding:")
    for claim in trace.claims:
        citations = ", ".join(claim.support_ids) if claim.support_ids else "no supporting passage"
        rows.append(f"  - [{claim.status}] {claim.text} (sources: {citations})")
    rows.append(f"Action: {trace.action.value}")
    rows.append("Customer-facing response:")
    rows.append(trace.answer)
    for note in trace.provenance_notes:
        rows.append("Provenance note: " + note)
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        sources = load_sources(args.policy, args.kb, args.transcript)
        rules = load_rules(args.rules)
        workflow = BillingWorkflow(sources, rules)
        if args.question:
            facts = snapshots_for_questions(sources)[-1][1] if args.use_demo_context and sources.customer_questions else None
            traces = [workflow.answer(args.question, facts)]
        else:
            traces = workflow.replay()
            if not traces:
                raise SourceLoadError("The transcript contains no customer question turns to replay.")
    except (SourceLoadError, RuleConfigError) as exc:
        print(f"Setup error: {exc}", file=sys.stderr)
        return 2

    metadata = _run_metadata(sources, workflow)
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"metadata": metadata, "turns": [trace.to_dict() for trace in traces]}
        args.json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Trace written to {args.json_path}")
    else:
        for warning in sources.warnings:
            print("Source warning: " + warning)
        for conflict in workflow.conflicts:
            print("Source conflict: " + conflict)
        print("NimbusCare offline policy-grounded workflow")
        print("No network access, account lookup, credit issuance, or external service was used.\n")
        for index, trace in enumerate(traces, 1):
            print(_render_trace(trace, index))
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
