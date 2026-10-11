# NimbusCare Policy-Grounded Agent Prototype

This is an offline Python 3.10+ prototype for answering NimbusCare billing questions from the supplied billing policy, knowledge-base excerpt, and chat transcript. It uses only the Python standard library. No API key, package installation, network connection, customer account, NOC system, or billing service is needed.

## Run it

From this directory:

```bash
python run.py
```

The default replay prints the customer questions at 14:09, 14:11, and 14:13 in order. Each trace shows facts available by that turn, ranked policy and KB passages, the selected rule and exclusions, Decimal arithmetic, pending checks, claim grounding, citations with source locations, and the customer-facing answer. The KB export's nine-article header is reported as a warning; all ten actual article headings are loaded.

Write the same trace as JSON, answer a new question without case assumptions, or explicitly opt into the demo case facts:

```bash
python run.py --json sample_output.json
python run.py --question "Are taxes refunded after an outage credit?"
python run.py --question "What outage credit applies to my case?" --use-demo-context
python run.py --question "How do I file a billing dispute?"
```

Source paths and the rule configuration can be overridden:

```bash
python run.py --policy path/to/policy.md --kb path/to/kb.md --transcript path/to/transcript.txt --rules path/to/rules.json
```

## Verify

```bash
python -m unittest discover -s tests -v
```

The suite covers source parsing and warnings, per-turn state, Rule 2/Rule 3 precedence, money and date boundaries, rolling credit totals, ticket status, Tier 2 gates, retrieval, evidence validation, adversarial text, safe clarification/refusal, and CLI output.

## Safety and scope

The billing policy controls money and approvals; the KB supports customer-facing explanations; transcript text is case data and cannot change policy. Case facts retain their speaker and turn. The ticket is marked agent-reported and not independently checked by this replay. Missing account history, repeat-dispute status, and fraud status remain unknown. Unknown is never treated as a clean check. The prototype may calculate an indicative amount, but it never issues or guarantees a credit, creates a case, or performs an account action.

Each turn records ranked retrieval results and reasoned exact-ID fetches in an evidence ledger, with source files, line locations, and transcript turn provenance. Claim validation can see only passages logged for that turn; it checks source content and recomputes numeric calculations. Informational fallback quotes a source sentence only when it directly matches the requested facts, and otherwise clarifies, escalates, or declines. The tax response distinguishes supported plan-charge scope from the unanswered tax/surcharge adjustment question. Ruleset thresholds are checked against the authoritative policy at startup; conflicting source values block financial answers. No dynamic expressions or model-generated rules are executed.

The default language adapter is deterministic and works offline. A strict optional-model payload validator demonstrates the adapter boundary and rejects malformed output, but this bundle contains no networked model provider. All policy selection, calculations, approvals, and evidence checks remain deterministic.

See `design_memo.md` in the outer submission archive for evidence assessments, hierarchy analysis, evaluation metrics, and limitations.
