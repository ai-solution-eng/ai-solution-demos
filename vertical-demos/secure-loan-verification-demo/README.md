# SLVD — Secure Loan Verification Demo

| Owner                 | Name    | Email           |
| ----------------------|---------|-----------------|
| Use Case Owner        | Eric Yu | eric.yu@hpe.com |
| PCAI Deployment Owner | Eric Yu | eric.yu@hpe.com |

## Abstract

A **governed, human-in-the-loop AI workflow** for loan renewal. A replication of marketing video [Human‑Guided AI: Secure Loan Verification on HPE Private Cloud AI](https://psnow.ext.hpe.com/#/tiles/multimedia?id=v100014756&preview=true&filter.textSearch=v100014756). An AI agent gathers
credit data from the bank's back-office systems and drafts a decision memo; a senior
credit officer makes the final call on large or risky cases. Every step is audited.

> **The story in one line:** the AI does the analyst's research work in minutes, but a
> human always holds the approval pen — and every action is on the record.

Features:
- **Governed AI** — the LLM agent prepares the work; a deterministic policy engine
  enforces approval rules; a human always makes the final call.
- **Five bank-system reads** via a governed MCP (per-case identity-checked, audit-logged)
  plus one **gated write** (memo cannot publish until an approval is recorded).
- **OpenClaw / NemoClaw agent** on PCAI with the `bank-credit` skill — the generative
  step runs as a real agent turn, not a scripted LLM call.
- **Live portal** — analyst UI, approver governance console, in-cluster Mailpit mailbox
  with signed approval-link emails; all roles see their own console.
- **Audit trail** — every step (who, when, which tool, what returned) is persisted per
  case and shown in the portal; the official memo is archived to a PVC per case/year.

Recordings:
- *(demo video to be sent at review — link will be added here)*

---

## Two ways to read this document

- **Business readers**: start with [Description](#description),
  then [Who does what](#who-does-what), then [Why it matters](#why-it-matters).
- **Technical readers**: jump to [Architecture](#architecture),
  [Deployment](#deployment-hpe-private-cloud-ai) and [Running the demo](#running-the-demo).

Both diagrams below are interactive HTML + static PNG:

| Diagram | Static | Interactive |
|---|---|---|
| Business perspective | [`images/slvd-business-perspective.png`](images/slvd-business-perspective.png) | [`images/slvd-business-perspective.html`](images/slvd-business-perspective.html) |
| Technical architecture | [`images/slvd-technical-architecture.png`](images/slvd-technical-architecture.png) | [`images/slvd-technical-architecture.html`](images/slvd-technical-architecture.html) |

---

## Description

### Overview

A client (Acme Industrial) asks to renew a **$5M** revolving facility. The bank's
analyst (Nick) starts the case. From there:

### Workflow

1. **Client requests** a renewal of the facility.
2. **Analyst submits** the case id and the requested amount.
3. **AI agent pulls data** from five bank systems — CRM profile, credit exposure,
   transaction behavior, compliance (KYC/sanctions), and the prior memo.
4. **AI drafts the memo** — a professional credit decision memo (recommendation + conditions).
5. **Policy gate** — if the amount is **≥ $5M** *or* KYC is **pending**, the case is escalated
   to a senior approver. (Smaller, clean cases can auto-complete.)
6. **Approver reviews & decides** — Sarah Chen, senior credit officer, approves or rejects
   from a governance console (or by clicking the link in her approval email).
7. **Memo is published** to the official credit record.
8. **Client is notified** by email with the outcome.

![Business perspective](images/slvd-business-perspective.png)

## Running the demo

### Demo1 — full governed run (screenshots from a live PCAI run)

Captured from a real run on the deployed demo (case `CR-2026-00451`, $5M renewal):

1. **The agent's `bank-credit` skill** — shipped by the NemoClaw chart and seeded into the
   gateway at pod start. The OpenClaw dashboard's Skills page lists it as *installed* and
   *eligible*; this is what turns the generic gateway into the governed credit analyst.

   ![NemoClaw skills page](images/nemoclaw-skills-bank-credit.png)

2. **Sign in as the analyst** — the portal login screen shows the demo credentials;
   `nick / analyst123` (Risk Analyst) and `sarah / officer123` (Senior Credit Officer)
   get different consoles, matching their roles.

   ![Portal login](images/portal-login.png)

3. **Nick starts the governed run** — he picks the case, states the amount, and clicks
   *Generate renewal decision memo*. The agent pulls the five bank systems via the
   governed MCP, drafts the memo, and the run reaches the **policy gate**
   (amount ≥ $5M + KYC pending) — it pauses here, and a signed approval email goes to Sarah:

   ![Run paused at the policy gate](images/nick-run-paused-at-gate.png)

4. **Sarah approves from her Governance Console** — the queue entry shows the case,
   the policy trigger, and the request timestamp; her decision is recorded under her
   identity (E200145) in the audit trail:

   ![Approval console](images/sarah-approval-console.png)

5. **The approval email in the mailbox** — Mailpit (the in-cluster SMTP sink,
   `https://slvd-mail.${DOMAIN_NAME}`) holds the `[ACTION REQUIRED]` email sent to
   `Sarah.Chen@mybank.com`: policy reasons, the case summary table, and a signed
   deep-link to record the decision from the email itself.

   ![Approval email in Mailpit](images/approval-email-mailpit.png)

6. **Nick's run completes — memo published** — after Sarah's decision the paused run
   auto-continues: every workflow step is checked, the memo is published to the official
   record (`/official/credit/2026/CR-2026-00451`), the decision summary names the
   approver, and the client receives the outcome email:

   ![Published memo for Nick's run](images/nick-published-memo.png)

### Who does what

| Role | Who | What they do | What they do **not** do |
|---|---|---|---|
| **Client** | Acme Industrial (CL-77821) | Requests the renewal; receives the decision by email | — |
| **Analyst** | Nick Johnson (E102938) | Starts the case, states the amount | Never approves anything |
| **AI Agent** | credit-memo-agent | Collects data from 5 systems, drafts the memo | Never invents a number; never makes the final call |
| **Approver** | Sarah Chen (E200145) | Makes the final approve/reject decision on large/risky cases | — |

### Why it matters

- **Governance** — AI prepares the work; a human makes the call. No AI decision is final
  on its own.
- **Accuracy** — every figure comes from a governed bank-system call. The agent is only
  allowed to cite real data; memos are validated before submission.
- **Auditability** — who did what, when, and why is recorded. The approval is logged under
  the approver's identity. The official memo is archived per case/year.
- **Efficiency** — the agent fetches and drafts in minutes, not days. The approver reviews a
  ready memo, not raw data. The client is notified the moment a decision lands.

---

## Architecture

### Technical overview

The system runs on **HPE Private Cloud AI (PCAI)** as pods in a dedicated Kubernetes
namespace (`slvd`), with the AI agent and the LLM as separate in-cluster services whose
endpoints are configured via chart values.

![Technical architecture](images/slvd-technical-architecture.png)

**Core components:**

| Component | Tech | Port | Responsibility |
|---|---|---|---|
| **Portal** | React + TS (nginx) | 80 | Analyst UI, approver console, mail inbox |
| **Workflow Engine** | FastAPI (Python 3.11) | 8080 | The governed pipeline, policy gate, JWT auth, audit, mailer |
| **credit-memo-mcp** | MCP server (JSON-RPC) | 8000 | Governed data tools + the gated submit tool |
| **Data Store** | SQLite + files on PVC | — | Runs, audit trail, draft/official memos |
| **Mailpit** | SMTP + UI | 1025 / 8025 | Approval + client email (in-cluster sink) |

**AI layer:**

| Component | Responsibility |
|---|---|
| **OpenClaw agent** (NemoClaw) | The generative step — pulls data via the `bank-credit` skill and drafts the memo |
| **litellm proxy** → **qwen3-8-27b** | LLM inference |

### Why both a FastAPI engine *and* an OpenClaw agent?

They are deliberately separated because an LLM agent is **not** a trustworthy system of record:

- **FastAPI = the bank's compliance system.** It is deterministic and unit-testable. It owns
  state, identity (analyst vs. approver JWT), the **policy gate** (`amount ≥ $5M OR KYC
  pending`), the **gated submit** (the memo cannot be published until an approval is recorded),
  the **audit trail**, and all human integration (portal, console, emails, official archive).
- **OpenClaw = the analyst's brain.** It is the visible "agentic" step — it reasons over the
  collected data and writes the memo. It is a *pluggable backend* for the drafting step
  (`SLVD_AGENT_BACKEND = openclaw | direct_llm | stub`); swap it and the entire governance
  layer is unchanged.
- **The agent cannot bypass control.** The submit tool is gated *server-side* in the MCP, and
  the agent's draft is *validated* (format + required values) before it proceeds. So no matter
  what the model wants to do, the memo only publishes after a human approval is recorded.

The demo's core point: **governed, human-in-the-loop AI** — the AI prepares the work, the
deterministic engine enforces policy, the human makes the call.

### The governed MCP tools

| Order | Tool | Returns | Access |
|---:|---|---|---|
| 1 | `get_crm_profile` | client profile, industry, BO status | read, per-case auth |
| 2 | `get_credit_exposure` | facility, limit, utilization, risk rating | read, per-case auth |
| 3 | `get_transactions` | transaction / cash-flow behavior | read, per-case auth |
| 4 | `get_compliance_status` | KYC status, sanctions | read, per-case auth |
| 5 | `get_prior_memo` | prior conditions, open follow-ups | read, per-case auth |
| 6 | `workflow__submit_credit_memo` | submits the memo into the workflow | **gated — needs approval** |

The agent calls all five read tools (safe), drafts the memo, then calls the gated submit.

> **Model note — llama3.1-8b is NOT supported in this demo (validated).** We validated `llama3.1-8b` and it is **not usable here**:
> its output does not fit the **accepted schema in OpenClaw** (the response/structure OpenClaw
> expects for agent turns), so the agent loop breaks. Use the qwen3 model.

---

## Repository layout

```
secure_loan_verification_demo/
├── source_code/         # all demo code
│   ├── engine/          # FastAPI workflow engine (pipeline, policy, audit, mailer, API)
│   │   ├── api.py       # REST endpoints (runs, approvals, audit, memo)
│   │   ├── workflow.py  # the 10-step governed pipeline
│   │   ├── policy.py    # amount >= $5M OR KYC pending rule
│   │   ├── mcp / agent / mailer / store / security / memo
│   │   └── tests/       # unit tests
│   ├── mcp-server/      # credit-memo-mcp (governed data tools + gated submit)
│   ├── portal/          # React + TS frontend (analyst UI, approver console, mail)
│   ├── skills/          # bank-credit skill (agent → governed MCP)
│   ├── e2e/             # 8-scenario end-to-end suite + cluster verify + demo-run
│   ├── mockdata/        # the five bank-system fixtures (CRM, credit, txn, compliance, memo)
│   ├── Makefile         # entry point: test / build / deploy / verify / clean
│   ├── requirements.txt # python deps for engine/e2e tooling
│   ├── Dockerfile       # engine + credit-memo-mcp shared image (context = repo root)
│   ├── Dockerfile.portal# portal (Vite build + nginx)
│   └── conftest.py      # pytest package resolution
├── charts/
│   ├── slvd/            # SLVD Helm chart + EzAppConfig (PCAI BYOA deploy)
│   └── nemoclaw/        # NemoClaw (OpenClaw agent) Helm chart — deployed first (prereq)
├── slvd-0.12.0.tgz      # packaged SLVD helm chart (at repo root)
├── nemoclaw-0.2.9.tgz   # packaged NemoClaw helm chart (at repo root)
├── nemoclaw-icon.png    # NemoClaw chart icon (UI app tile logo)
├── slvd-icon.png        # SLVD app icon (UI app tile logo)
└── images/              # screenshots + architecture/business diagrams (html + png)
```

---

## Deployment (HPE Private Cloud AI)

The demo is deployed by **importing the framework through the PCAI platform UI**.
The UI does the install:
Upload the chart via the UI wizard and applies the values you enter in the form.

### Prerequisites

The SLVD engine drives the agent through the **NemoClaw / OpenClaw gateway**, so the
NemoClaw chart must be deployed **before** the SLVD chart (the engine connects to it via
`SLVD_OPENCLAW_URL`). It is shipped in this repo:

- Chart source: `charts/nemoclaw/` (Helm chart, icon at `nemoclaw/icon.png`)
- Packaged: `nemoclaw-0.2.9.tgz` (repo root)
- Skills: the chart ships `files/skills/bank-credit/SKILL.md` and seeds it into
  the agent state dir at every pod start — the OpenClaw agent runs headless, so
  without this the `bank-credit` skill is missing and memo drafting degrades.
- Icon: `nemoclaw-icon.png` (repo root — used as the UI app-tile logo)

Push it to chartmuseum, then in the PCAI UI: Frameworks → *Import framework* →
select the `nemoclaw` chart → fill in values (`ezua.virtualService.endpoint` with
`${DOMAIN_NAME}`, the LLM endpoint `litellm.baseUrl` + `litellm.model` + `litellm.apiKey`
(API key is entered directly in the values form), `gateway.token`) → *Deploy*.
This creates the `nemoclaw` namespace + the OpenClaw gateway service that SLVD targets.

### Installation and configuration

1. **Import in the PCAI UI**: Frameworks → *Import framework* (BYOA) → select the
   `slvd` chart version from chartmuseum → upload `slvd-icon.png` as the app-tile
   logo → fill in the values:
   - image tag (from step 1) and `${DOMAIN_NAME}` for the ingress host
   - the `secrets:` block no longer exists — the form can be **left empty**
     (zero-input deploy). Backend credentials now live under `engine.env`
     and are emitted only for the configured `SLVD_AGENT_BACKEND`:
     `SLVD_LLM_API_KEY` (backend=`direct_llm`, auto-detected from the
     in-cluster litellm master-key Secret when empty) and
     `SLVD_OPENCLAW_TOKEN` (backend=`openclaw`, auto-detected from the
     NemoClaw gateway ConfigMap when empty). Signing keys
     (`SLVD_JWT_SECRET`/`SLVD_HMAC_SECRET`/`SLVD_MCP_INTERNAL_TOKEN`) are
     generated deterministically from the release coordinates. Any non-empty
     `engine.env` value you enter always overrides auto-detection.
   - agent/LLM endpoints (`SLVD_OPENCLAW_URL`, `SLVD_LLM_BASE_URL`) are
     auto-detected from the in-cluster NemoClaw / LiteLLM services when those
     apps are deployed first; the values.yaml entries are fallbacks only.

   → *Deploy*. The platform creates the namespace and the release.


- **External URLs**: an Istio VirtualService on the platform ingress gateway
  (`istio-system/ezaf-gateway`) exposes
  - Portal: `https://slvd.${DOMAIN_NAME}`
  - Mail:   `https://slvd-mail.${DOMAIN_NAME}`
- **Pods**: exactly 4 (1 per component) — portal, engine, credit-memo-mcp, mailpit.
- **Agent backend**: `SLVD_AGENT_BACKEND=openclaw` → the NemoClaw gateway service.
- **LLM**: `SLVD_LLM_MODEL` via the litellm proxy at `SLVD_LLM_BASE_URL`.

### Default users (demo)

| Role | Username | Password |
|---|---|---|
| Analyst | `nick` | `analyst123` |
| Approver | `sarah` | `officer123` |

---

## Verification
- **Live**: a real run through the openclaw agent → governed MCP → approval gate →
  approver decision → published memo → client email, with every step in the audit trail.

## Limitations

- **Demo credentials** (`nick/analyst123`, `sarah/officer123`) are fixed users —
  not an identity provider. Fine for a demo; a real deployment needs SSO/LDAP.
- **Mock bank data** — the five "bank systems" are fixture-backed MCP tools
  (`source_code/mockdata/`); no external systems are touched.
- **Mailpit** is an in-cluster mail sink: approval/client emails never leave the
  platform, and its store is in-memory per pod (emails are cleared on pod restart;
  the signed approval-link flow still works from the portal console).
- **Model note** — the agent runs on the validated `qwen3-8-27b-int4` alias via
  litellm; a general instruction model (e.g. llama3.1-8b) does NOT fit OpenClaw's
  response schema for this workflow and breaks the agent loop.
- The demo assumes the NemoClaw + litellm prereqs are imported FIRST (chart order:
  nemoclaw → slvd); SLVD auto-detects their endpoints in-cluster when left unset.

## License

Internal demo.
