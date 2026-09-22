"""A8 security gate:
1. Secret scan of the repo (gitleaks if available, else built-in pattern scan).
2. Rendered-chart scan: `helm template` output must not contain any non-empty
   secret value (secrets must be empty/placeholder in committed values).
3. Gitignore check: the gitignored EzAppConfig (real values incl. secrets) must be excluded.
Exit 0 = clean.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

# High-signal secret patterns (avoid false positives on keys like "apiKey" field names).
SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|apikey|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{24,}['\"]?"), "credential assignment"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "openai-style key"),
    (re.compile(r"(?i)eyJhbGci[A-Za-z0-9\-_\.]{40,}"), "JWT"),
    (re.compile(r"(?i)BEGIN (RSA|EC|OPENSSH) PRIVATE KEY"), "private key block"),
]

ALLOWLIST = {
    "IMPLEMENTATION_PLAN.md",  # mentions patterns in prose
    "charts/slvd/ezappconfig.generated.yaml",  # gitignored; holds real values locally (deploy trigger)
    "e2e/security_scan.py",
    "venv/",
    "node_modules/",
    "dist/",
    "logs/",
    "frames/",
    ".git/",
}

SKIP_EXT = {".mp4", ".png", ".jpg", ".tgz", ".db", ".sqlite", ".node", ".wasm", ".gz"}


def _ignored(rel: str) -> bool:
    parts = rel.split(os.sep)
    for a in ALLOWLIST:
        a = a.rstrip("/")
        if a in parts:
            return True
        if rel.startswith(a + "/") or rel == a:
            return True
    return False


def iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # prune ignored dirs in-place for speed
        rel_dir = os.path.relpath(dirpath, ROOT)
        dirnames[:] = [d for d in dirnames if not _ignored(os.path.join(rel_dir, d) if rel_dir != "." else d)]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, ROOT)
            if _ignored(rel):
                continue
            if os.path.splitext(fn)[1] in SKIP_EXT:
                continue
            yield p, rel


def scan_repo() -> list[str]:
    findings = []
    try:
        r = subprocess.run(["gitleaks", "detect", "--source", ROOT, "--no-banner", "-r"],
                           capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            return []
        for line in (r.stdout or "").splitlines():
            if line.strip():
                findings.append(f"gitleaks: {line.strip()}")
        return findings
    except FileNotFoundError:
        pass
    # Fallback pattern scan
    for p, rel in iter_files():
        try:
            data = open(p, "rb").read()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue
        text = data.decode("utf-8", "replace")
        for i, line in enumerate(text.splitlines(), 1):
            for pat, label in SECRET_PATTERNS:
                m = pat.search(line)
                if m:
                    # filter obvious non-secrets (field names without values, examples)
                    if re.search(r"(?i)example|placeholder|__|<|your[-_]", line):
                        continue
                    findings.append(f"{rel}:{i} [{label}] {line.strip()[:120]}")
    return findings


def scan_rendered_chart() -> list[str]:
    findings = []
    r = subprocess.run(["helm", "template", "slvd", os.path.join(ROOT, "charts", "slvd")],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return [f"helm template failed: {r.stderr[:300]}"]
    # committed values must keep secrets empty; rendered chart must not leak values
    for i, line in enumerate(r.stdout.splitlines(), 1):
        for pat, label in SECRET_PATTERNS:
            if pat.search(line):
                findings.append(f"rendered-chart L{i} [{label}]: {line.strip()[:120]}")
    # explicitly: stringData values must be empty or obviously non-secret
    m = re.search(r"stringData:\n((?:\s{2}\S.*\n?)+)", r.stdout)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                v = val.strip().strip('"')
                if v and not re.match(r"^(dev-|test-)", v) and len(v) > 12:
                    findings.append(f"rendered-chart stringData {key.strip()} = {v[:12]}... (looks real)")
    return findings


def gitignore_ok() -> list[str]:
    # No root .gitignore in this repo (by design) — ignore rules live in
    # .git/info/exclude. The critical one: the generated EzAppConfig carries
    # real secrets and must never be committed.
    gi = os.path.join(ROOT, ".git", "info", "exclude")
    findings = []
    if not os.path.exists(gi):
        return [".git/info/exclude missing"]
    txt = open(gi).read()
    for req in ["ezappconfig.generated.yaml", "venv/", "node_modules/", "*.db"]:
        if req not in txt:
            findings.append(f".git/info/exclude missing entry: {req}")
    return findings


def main():
    all_findings: list[str] = []
    all_findings += [f"[repo] {x}" for x in scan_repo()]
    all_findings += [f"[rendered-chart] {x}" for x in scan_rendered_chart()]
    all_findings += [f"[gitignore] {x}" for x in gitignore_ok()]
    if all_findings:
        print(json.dumps({"ok": False, "findings": all_findings}, indent=2))
        sys.exit(1)
    print(json.dumps({"ok": True}))
    sys.exit(0)


if __name__ == "__main__":
    main()
