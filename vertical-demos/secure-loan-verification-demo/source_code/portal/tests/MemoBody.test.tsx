import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import MemoBody, { parseMemo } from "../src/MemoBody";

const STUB_MEMO = `# Loan Renewal Decision Memo

**Client:** Acme Industrial Holdings (CL-77821) — Renewal Request

## Overview
- Requested renewal amount: $5,000,000
- Current utilization: $3,800,000 (76% of limit)
- Facility type: Revolving credit
- Risk rating: BB
- Status: In compliance with covenants

## Key Considerations
1. Stable transaction history with no delinquencies
2. Pending KYC review (beneficial ownership update required)
3. Prior memo conditions: Quarterly reporting and no additional unsecured borrowing
4. Open follow-up: Collateral valuation update needed

## Recommended Decision
Approve with conditions — renewal of $5,000,000 is recommended, subject to completion of the pending KYC review and the open follow-up. Prior conditions carry forward.
`;

describe("parseMemo", () => {
  it("extracts client line", () => {
    const { client } = parseMemo(STUB_MEMO);
    expect(client).toBe("Acme Industrial Holdings (CL-77821)");
  });
  it("splits the three canonical sections in order", () => {
    const { sections } = parseMemo(STUB_MEMO);
    expect(sections.map((s) => s.title)).toEqual([
      "Overview",
      "Key Considerations",
      "Recommended Decision",
    ]);
  });
  it("keeps all overview fields", () => {
    const { sections } = parseMemo(STUB_MEMO);
    const overview = sections.find((s) => s.title === "Overview")!;
    expect(overview.items.join(" ")).toContain("$5,000,000");
    expect(overview.items.join(" ")).toContain("76% of limit");
    expect(overview.items.join(" ")).toContain("BB");
  });
  it("keeps 4 numbered key considerations", () => {
    const { sections } = parseMemo(STUB_MEMO);
    const kc = sections.find((s) => s.title === "Key Considerations")!;
    expect(kc.items).toHaveLength(4);
  });
});

describe("MemoBody", () => {
  it("renders the client line and all sections", () => {
    render(<React.StrictMode><MemoBody md={STUB_MEMO} /></React.StrictMode>);
    expect(screen.getByText("Client: Acme Industrial Holdings (CL-77821) — Renewal Request")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Key Considerations" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Recommended Decision" })).toBeInTheDocument();
    expect(document.body.textContent).toContain("$3,800,000 (76% of limit)");
  });
  it("renders an empty memo without crashing", () => {
    render(<MemoBody md="" />);
    expect(document.body).toBeTruthy();
  });
});
