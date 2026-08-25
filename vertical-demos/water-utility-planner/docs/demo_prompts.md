# Demo Prompts — Water Utility Tree Root Risk Ranker

Copy-paste ready SQL and chat prompts for running the demo.

**Total demo time**: ~10-13 minutes
**Structure**: Act 1 (Query Editor, 3-4 min) → Act 2 (Chat, 5-6 min) → optional bonus depth

---

## Table of contents

1. [Act 1 — Query Editor (developer walkthrough)](#act-1--query-editor-developer-walkthrough)
2. [Act 2 — Open WebUI chat (user walkthrough)](#act-2--open-webui-chat-user-walkthrough)
3. [Bonus prompts (backup / depth)](#bonus-prompts-backup--depth)
4. [Rehearsal warnings](#rehearsal-warnings)
5. [Pre-demo checklist](#pre-demo-checklist)

---

## Act 1 — Query Editor (developer walkthrough)

Run these in **AIE UI → Query Editor** while walking through the platform tour. Each block lands a specific message.

### 1. Row-count baseline — the data is real

```sql
SELECT COUNT(*) FROM waterutility.default.pipes;
-- Expected: 2000
```

```sql
SELECT COUNT(*) FROM waterutility.default.work_orders;
-- Expected: ~340
```

*Message*: Two parquet files on shared storage, registered as EzPresto external tables. No ETL pipeline, no data movement.

### 2. Schema check — federation is over structure, not just files

```sql
SHOW COLUMNS FROM waterutility.default.pipes;
```

*Message*: EzPresto knows the schema. Any SQL client can query it — Python, BI tools, or an AI agent.

### 3. District breakdown — planners can slice however they like

```sql
SELECT district_code, COUNT(*) AS pipe_count, AVG(age_years) AS avg_age_years
FROM waterutility.default.pipes
GROUP BY district_code
ORDER BY pipe_count DESC;
-- Expected: NORTH / SOUTH / EAST / WEST with ~500 pipes each
```

### 4. Material distribution — the asset mix

```sql
SELECT material, COUNT(*) AS cnt,
       ROUND(AVG(age_years), 1) AS avg_age,
       SUM(has_root_incident_5y) AS confirmed_incidents
FROM waterutility.default.pipes
GROUP BY material
ORDER BY cnt DESC;
```

*Message*: Vitrified clay pipes (VC) dominate the register and account for the majority of incidents — matches published research.

### 5. The federation query ⭐ — join model data to operational data

```sql
SELECT COUNT(*) AS never_inspected
FROM waterutility.default.pipes p
LEFT JOIN waterutility.default.work_orders w
  ON p.pipe_id = w.pipe_id
WHERE w.work_order_id IS NULL;
-- Expected: ~1660
```

*Message*: 1,660 pipes have never been inspected. This LEFT JOIN takes ~1.6 seconds against real Presto — it's the same query the AI agent will run in chat.

### 6. Spend analysis — any operational question

```sql
SELECT crew,
       COUNT(*) AS jobs,
       SUM(cost_aud) AS total_spend_aud,
       ROUND(AVG(cost_aud), 0) AS avg_cost_per_job
FROM waterutility.default.work_orders
WHERE work_type = 'REPAIR_ROOT_INTRUSION'
GROUP BY crew
ORDER BY total_spend_aud DESC;
```

*Message*: This is the kind of question a general manager asks. SQL answers it. So does the agent, in plain English.

### 7. Age-vs-incidents correlation — the training signal is real

```sql
SELECT
  CASE
    WHEN age_years < 30  THEN '0-30 years'
    WHEN age_years < 60  THEN '30-60 years'
    WHEN age_years < 90  THEN '60-90 years'
    ELSE '90+ years'
  END AS age_band,
  COUNT(*) AS pipes,
  ROUND(AVG(has_root_incident_5y) * 100, 1) AS incident_rate_pct
FROM waterutility.default.pipes
GROUP BY 1
ORDER BY 1;
```

*Message*: Older pipes have measurably higher incident rates in the training data. This is what the ML model learns.

---

## Act 2 — Open WebUI chat (user walkthrough)

**Setup**: Fresh chat → model selector → **Water Utility Planner** (the custom Model preset). All four tools are pre-enabled by the preset. No manual tool toggling needed.

### Beat 1 — Opening query (`rank_pipes` fires)

```
Which pipes in the northern district should I CCTV inspect next quarter?
Budget for 50 inspections.
```

**Expected trace**: `rank_pipes(district="NORTH", top_k=50, budget=50)` → table of 50 pipes sorted by risk score.

### Beat 2 — Per-pipe justification (`explain_pipe` fires)

Explicit reference (safer):

```
Explain why P-000123 is the highest risk.
```

*(Substitute the actual top pipe_id from Beat 1's response.)*

Implicit reference (relies on the LLM's context memory):

```
Explain why the top one is risky.
```

**Expected trace**: `explain_pipe(pipe_id="P-000123")` → SHAP contributions in plain English.

### Beat 3 — Feature-importance narrative (no tool call)

```
Which factors is your model relying on most for its risk scores?
Are those the drivers I'd expect for tree root infiltration?
```

**Expected**: LLM summarises across Beat 2's contributions — names age, tree proximity, historical incidents. **No tool call** — pure reasoning over prior context.

### Beat 4 — Federation moment ⭐ (`ezpresto_sql` fires)

```
Of the 50 pipes you just ranked, how many have never had a work order logged?
```

**Expected trace**: `ezpresto_sql` fires with a LEFT JOIN. Returns count for NORTH pipes only.

**Broader alternative** (returns the full ~1,660 across all districts):

```
How many pipes in our entire register have never had a work order logged?
```

### Beat 5 — Graceful degradation (LLM declines cleanly)

```
Now filter that ranked list to only pipes within 200 metres of a school.
```

**Expected**: LLM says the data doesn't include school proximity — offers to work with what's there. **Does NOT invent a filter.**

### Beat 6 — Optional model swap

Change the model picker to `gemma-4-31b-ab`, then rerun Beat 1's prompt verbatim:

```
Which pipes in the northern district should I CCTV inspect next quarter?
Budget for 50 inspections.
```

**Expected**: same tool chain, functionally equivalent output. Evidence that the scaffolding is model-agnostic.

---

## Bonus prompts (backup / depth)

For audiences who ask deeper questions during the demo.

### "Show me the underlying data"

```
Show me 5 sample pipes from the WEST district with all their attributes.
```

**Expected**: `query_pipes(district="WEST", limit=5)` — no ranking, just a browse.

### "What's our worst-performing crew?"

```
Which crew has the highest average cost per root-intrusion repair?
```

**Expected**: `ezpresto_sql` with a GROUP BY on `work_orders`.

### "How many high-risk pipes exist across all districts?"

```
Give me the top 3 pipes to inspect in each district — 12 pipes total.
```

**Expected**: agent calls `rank_pipes` four times (NORTH, SOUTH, EAST, WEST) with `top_k=3`. Multi-tool orchestration in one prompt.

### "Are we spending on the right pipes?"

```
Cross-reference: of the 20 highest-risk pipes in NORTH,
how many already have work orders logged this year?
```

**Expected**: agent chains `rank_pipes` then `ezpresto_sql` with a JOIN filtered by pipe_id list and date range. Complex — a good "wow" if it lands, but rehearse before showing.

### "What if I only care about VC pipes?"

```
Of the pipes we've flagged as high-risk in NORTH,
which are vitrified clay? Those are our brittle-material priority.
```

**Expected**: `rank_pipes` then LLM filters the response by material. May trigger `ezpresto_sql` with `WHERE material = 'VC'` depending on the model's choice.

---

## Rehearsal warnings

Three things to test before showing the demo live.

### 1. Beat 4 — the federation moment

This is the demo's highest-impact beat. If the LLM picks `query_pipes` instead of `ezpresto_sql`, the JOIN doesn't happen and the whole federation story flattens. **Run this beat three times in your dry run.** If it picks wrong repeatedly, tighten the system prompt in the Water Utility Planner preset to more strongly prefer `ezpresto_sql` for JOIN-shaped questions.

### 2. Beat 5 — graceful decline

Do not correct the LLM when it declines gracefully. The message the audience takes away is "this system won't make things up," which is worth more than any risk score. **Practise letting the silence land after a decline** — it's uncomfortable in rehearsal but reads as confidence live.

### 3. The `USING` vs `ON` pitfall

If you improvise a query in Act 1, always use:

```sql
LEFT JOIN work_orders w ON p.pipe_id = w.pipe_id
WHERE w.work_order_id IS NULL
```

**Never**:

```sql
LEFT JOIN work_orders w USING (pipe_id)
WHERE w.pipe_id IS NULL   -- ❌ Presto: 'w.pipe_id' cannot be resolved
```

`USING` merges the join key — after the join there is no separate `w.pipe_id` to filter on. Presto will throw a live error on stage. Filter on a non-key right-side column (`w.work_order_id`) instead.

---

## Pre-demo checklist

Run this sequence 5 minutes before the meeting starts:

- [ ] Test the Beat 1 prompt in a fresh chat, confirm `rank_pipes` returns real pipes
- [ ] Test the Beat 4 prompt, confirm the federation JOIN returns a number (~1660)
- [ ] Confirm the MLIS endpoint responds `200` on `/healthz`
- [ ] Load Query Editor with query #1 pre-typed but not run — smooth handoff into Act 1
