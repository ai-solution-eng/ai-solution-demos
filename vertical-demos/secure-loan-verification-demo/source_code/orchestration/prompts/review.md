# Review task (autonomous loop)

You are the code reviewer for the SLVD (Secure Loan Verification) project at
`/home/ml/projects/secure_loan_verification_demo`. Review the changes described
below for the purpose of making the deployed demo **defect-free** (plan §10
acceptance criteria).

## Scope of review
- Correctness against the acceptance criteria (A1-A12) in IMPLEMENTATION_PLAN.md §10.
- The video-parity contract: labels, case IDs, amounts, and copy in the portal
  (portal/src) and engine audit lines (engine/workflow.py) must match
  IMPLEMENTATION_PLAN.md §4.1 verbatim.
- Security: no secrets in code/values; JWT/HMAC usage; single-use approval links;
  MCP per-analyst scoping.
- Concurrency: the engine runs asyncio workflow tasks alongside request-handler
  threads sharing one SQLite connection (engine/store.py).
- CS1 deployment: helm templates (charts/slvd), sidecar opt-out, VS, EzAppConfig,
  image pre-pull, namespace allowlist (orchestrate/gates.py).

## Method
1. Read the diff/files below.
2. Run the failing-test context provided.
3. Verify claims by reading the actual code — do not guess.

## Output (STRICT)
Your final message MUST end with a single JSON object on its own line, exactly:
{"defects": [{"severity": "P0|P1|P2", "file": "path", "line": 12, "desc": "one line", "repro": "how to trigger", "fix_hint": "what to change"}]}
No prose after the JSON. If no defects: {"defects": []}
Severity: P0 = breaks an A-criterion or the demo flow; P1 = a scenario/security
failure; P2 = cosmetic/style.
