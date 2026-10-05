# Fix task (autonomous loop)

You are the fix engineer for the SLVD project at
`/home/ml/projects/secure_loan_verification_demo` (venv at ./venv).

You are given a defect list (JSON) and, where present, the failing test output.
Fix ALL P0 and P1 defects. P2 only if trivial.

## Rules
- Fix root causes, not symptoms. Do not weaken or delete tests to make them pass —
  a test that encodes plan §10 acceptance criteria is correct; fix the code.
- If a test itself is wrong (contradicts IMPLEMENTATION_PLAN.md §10), you may fix the
  test, but only after quoting the plan line that proves the test is wrong.
- After fixing, run the relevant tests yourself:
  - python unit:  ./venv/bin/python -m pytest engine/tests/ mcp-server/tests/ -q
  - e2e:          ./venv/bin/python e2e/run_e2e.py [S1..S8]
  - portal:       cd portal && npx vitest run && npm run build
- Keep changes minimal and idiomatic. No new dependencies unless a defect requires it.
- Do NOT touch: git history, the autonomous loop (orchestrate/loop.py), or any
  resource outside this project directory.
- Do NOT deploy to the cluster (the loop handles deployment).

## Output
End with one line: FIXED <count> defects: <one-line summary per defect>.
