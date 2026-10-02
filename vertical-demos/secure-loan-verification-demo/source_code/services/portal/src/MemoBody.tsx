import React from "react";

export function parseMemo(md: string): { client: string; sections: { title: string; items: string[] }[] } {
  const client = md.match(/^\*{0,2}Client:\*{0,2}\s+(.*) — Renewal Request$/m)?.[1] || "";
  const sections: { title: string; items: string[] }[] = [];
  let section: { title: string; items: string[] } | null = null;
  for (const raw of md.split("\n")) {
    const ln = raw.trim();
    if (!ln) continue;
    if (ln.startsWith("# Loan Renewal")) continue;
    if (/^\*{0,2}Client:\*{0,2}.*— Renewal Request$/.test(ln)) continue;
    if (/^##/.test(ln)) {
      section = { title: ln.replace(/^##\s*/, "").trim(), items: [] };
      sections.push(section);
      continue;
    }
    const content = ln.replace(/^\*{0,2}/, "").replace(/\*{0,2}\s*$/, "").replace(/^([-*]|\d+\.)\s+/, "");
    if (!section) { section = { title: "Overview", items: [] }; sections.push(section); }
    section.items.push(content);
  }
  return { client, sections };
}

export function MemoBody({ md }: { md: string }) {
  const { client, sections } = parseMemo(md);
  return (
    <div>
      {client && <div className="client-line">Client: {client} — Renewal Request</div>}
      {sections.map((s) => (
        <div key={s.title} data-section={s.title}>
          <h3>{s.title}</h3>
          <ul>
            {s.items.map((it, i) => <li key={i}>{it}</li>)}
          </ul>
        </div>
      ))}
    </div>
  );
}

export default MemoBody;
