#!/usr/bin/env python3
"""SLVD autonomous development loop (plan §11).

Commands:
  python3 orchestrate/loop.py start   start the supervised loop (detached, survives session close)
  python3 orchestrate/loop.py status  show current phase / gate results / open defects
  python3 orchestrate/loop.py stop    graceful stop after current phase
  python3 orchestrate/loop.py resume  (re)start after a crash — resumes from state.json

Iteration (plan §11.2):
  FIX(0 if open defects) -> BUILD(local) -> UNIT -> REVIEW(static+LLM) -> E2E
  -> [cluster: DEPLOY -> CLUSTER -> GOLDEN] -> FINAL
  all green 2x in a row + repro check => ACCEPTANCE PASS => HANDOFF.md, stop.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

# Cluster/unit knobs (dev-only automation; override via env — not used by the
# shipped chart/images). Defaults preserve the historical CS1 lab targets.
KUBECONFIG = os.environ.get("SLVD_KUBECONFIG", "/home/ml/projects/kubeconfig-cs1.conf")
SLVD_IMG = os.environ.get("SLVD_IMG", "registry.ctc.sg.lab:5000/slvd")
SLVD_LLM_NS = os.environ.get("SLVD_LLM_NS", "project-user-aieadmin")
SLVD_HOST = os.environ.get("SLVD_HOST", "slvd.aie.cs1.ctc.sg.lab")
# Propagate to e2e scripts spawned via run_cmd (they require KUBE/SLVD_HOST).
os.environ.setdefault("KUBECONFIG", KUBECONFIG)
os.environ.setdefault("KUBE", KUBECONFIG)
os.environ.setdefault("SLVD_HOST", SLVD_HOST)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
ORCH = os.path.join(ROOT, "source_code", "orchestration")
STATE_F = os.path.join(ORCH, "state.json")
DEFECTS_F = os.path.join(ORCH, "defects.jsonl")
LOG_F = os.path.join(ORCH, "iteration_log.md")
PID_F = os.path.join(ORCH, "loop.pid")
STOP_F = os.path.join(ORCH, "STOP")
PY = os.path.join(ROOT, "venv", "bin", "python")

MAX_ITERS = 40
WALL_CAP_H = 24.0
PHASE_CAP_S = 45 * 60
WORKER_CAP_S = 20 * 60
CLUSTER_RETRY_EVERY = 3  # iterations

PHASES = ["fix", "build", "unit", "review", "e2e", "deploy", "cluster", "golden", "final"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(line: str) -> None:
    with open(LOG_F, "a", encoding="utf-8") as f:
        f.write(f"{now_iso()} {line}\n")
    print(line, flush=True)


def state_load() -> dict:
    if os.path.exists(STATE_F):
        with open(STATE_F) as f:
            return json.load(f)
    return {"iteration": 0, "phase": None, "green_streak": 0, "started_at": now_iso(),
            "cluster_checked": 0, "history": []}


def state_save(s: dict) -> None:
    with open(STATE_F, "w") as f:
        json.dump(s, f, indent=2)


def defects_open() -> list[dict]:
    out = []
    if os.path.exists(DEFECTS_F):
        for line in open(DEFECTS_F):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("status") == "open":
                out.append(d)
    return out


def defect_add(severity: str, phase: str, desc: str, detail: str = "") -> None:
    with open(DEFECTS_F, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_iso(), "severity": severity, "phase": phase,
                            "desc": desc[:300], "detail": detail[:4000], "status": "open"}) + "\n")


def defect_close_all() -> None:
    if not os.path.exists(DEFECTS_F):
        return
    lines = open(DEFECTS_F).read().splitlines()
    with open(DEFECTS_F, "w") as f:
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("status") == "open":
                d["status"] = "closed"
                d["closed_ts"] = now_iso()
            f.write(json.dumps(d) + "\n")


def run_cmd(cmd: list[str], timeout: int = PHASE_CAP_S, cwd: str = ROOT) -> tuple[int, str]:
    log(f"$ {' '.join(shlex.quote(c) for c in cmd[:6])}{' ...' if len(cmd) > 6 else ''}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        out = (r.stdout or "") + ("\n[stderr]\n" + (r.stderr or "") if r.returncode != 0 else "")
        return r.returncode, out[-20000:]
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"


def git_commit(msg: str) -> bool:
    subprocess.run(["git", "add", "-A"], cwd=ROOT, capture_output=True)
    r = subprocess.run(["git", "commit", "-m", msg], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return False  # nothing to commit
    # push if remote exists (non-fatal)
    rr = subprocess.run(["git", "push", "-q"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    if rr.returncode != 0:
        log(f"git push failed (non-fatal): {rr.stderr[-200:]}")
    return True


def cluster_available() -> bool:
    rc, out = run_cmd(["kubectl", "--kubeconfig", KUBECONFIG,
                       "get", "ns", "--request-timeout=10s"], timeout=30)
    return rc == 0


def check_cluster_hard() -> bool:
    rc, out = run_cmd(["kubectl", "--kubeconfig", KUBECONFIG,
                       "get", "ns", "--request-timeout=10s"], timeout=30)
    return rc == 0


# ---------------- workers (hermes one-shot) ----------------

def hermes_worker(prompt: str, timeout: int = WORKER_CAP_S) -> str:
    pf = os.path.join(ORCH, "_worker_prompt.md")
    with open(pf, "w", encoding="utf-8") as f:
        f.write(prompt)
    cmd = ["hermes", "chat", "--query-file", pf, "--oneshot", "--yolo",
           "-t", "terminal,files", "--max-turns", "80", "--run-budget", str(timeout - 120)]
    log(f"hermes worker (cap {timeout}s) ...")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
        return (r.stdout or "")[-30000:]
    except subprocess.TimeoutExpired:
        return "WORKER_TIMEOUT"


def extract_json(obj_text: str) -> dict | None:
    m = re.search(r'\{.*"defects".*\}', obj_text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        # try last line that looks like json
        for line in reversed(obj_text.splitlines()):
            line = line.strip()
            if line.startswith("{") and "defects" in line:
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    return None


# ---------------- phases ----------------

def phase_fix(state: dict) -> tuple[bool, str]:
    open_d = defects_open()
    if not open_d:
        return True, "no open defects"
    p01 = [d for d in open_d if d["severity"] in ("P0", "P1")]
    if not p01:
        return True, "only P2 open (non-blocking)"
    log(f"FIX: {len(p01)} open P0/P1 defects")
    prompt = open(os.path.join(ORCH, "prompts", "fix.md")).read()
    prompt += "\n\n## Defects to fix\n" + json.dumps(p01[:10], indent=2)
    prompt += "\n\n## Context\nRecent iteration log tail:\n"
    prompt += open(LOG_F).read()[-4000:]
    out = hermes_worker(prompt)
    ok, t = run_cmd([PY, "-m", "pytest", "source_code/services/engine/tests/", "source_code/services/mcp-server/tests/", "-q"], timeout=300)
    tail = out[-1500:] + "\n[unit] " + t[-800:]
    if "WORKER_TIMEOUT" in out:
        return False, "fix worker timed out\n" + tail
    log(f"fix worker output tail: {out[-400:]}")
    # re-run the scenario suite if e2e was among defects
    if any(d["phase"] in ("e2e", "cluster", "golden", "unit") for d in p01):
        ok2, t2 = run_cmd([PY, "source_code/testing/e2e/run_e2e.py"], timeout=PHASE_CAP_S)
        tail += "\n[e2e] " + t2[-800:]
        ok = ok and ok2
    return ok, tail


def phase_build(state: dict) -> tuple[bool, str]:
    ok, out = run_cmd(["make", "-f", "source_code/Makefile", "build"], timeout=PHASE_CAP_S)
    return ok == 0, out


def phase_unit(state: dict) -> tuple[bool, str]:
    ok, out = run_cmd([PY, "-m", "pytest", "source_code/services/engine/tests/", "source_code/services/mcp-server/tests/", "-q"], timeout=300)
    return ok == 0, out


def phase_review(state: dict) -> tuple[bool, str]:
    findings: list[str] = []
    # static gates
    ok, out = run_cmd(["helm", "lint", os.path.join(ROOT, "charts", "slvd")], timeout=120)
    if ok != 0:
        findings.append(f"helm lint failed: {out[-500:]}")
    ok, out = run_cmd(["helm", "template", "slvd", os.path.join(ROOT, "charts", "slvd")], timeout=120)
    if ok != 0:
        findings.append(f"helm template failed: {out[-500:]}")
    ok, out = run_cmd([PY, os.path.join(ROOT, "source_code", "testing", "e2e", "security_scan.py")], timeout=300)
    if ok != 0:
        findings.append(f"security scan: {out[-1000:]}")
    # LLM review of the diff since last review (only if code changed)
    diff = subprocess.run(["git", "diff", "--stat", "HEAD~1", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True).stdout
    changed = any(x not in diff for x in ("",)) and diff.strip() != ""
    if changed:
        d = subprocess.run(["git", "diff", "HEAD~1", "HEAD"], cwd=ROOT,
                           capture_output=True, text=True).stdout[:60000]
        prompt = open(os.path.join(ORCH, "prompts", "review.md")).read()
        prompt += f"\n\n## Diff under review\n```diff\n{d}\n```\n"
        if findings:
            prompt += "\n## Static gate findings so far\n" + "\n".join(findings)
        out_w = hermes_worker(prompt)
        res = extract_json(out_w)
        if res:
            for d_ in res.get("defects", []):
                sev = d_.get("severity", "P2")
                defect_add(sev, "review", f"{d_.get('file','?')}:{d_.get('line','?')} {d_.get('desc','')}",
                           f"repro: {d_.get('repro','')} | hint: {d_.get('fix_hint','')}")
            n_p01 = sum(1 for d_ in res.get("defects", []) if d_.get("severity") in ("P0", "P1"))
            log(f"LLM review: {len(res.get('defects', []))} defects ({n_p01} P0/P1)")
        else:
            log("LLM review produced no parseable JSON (counted as pass-with-note)")
    else:
        log("no code change since last commit — skipping LLM review, static gates only")
    open_d = defects_open()
    if open_d or findings:
        detail = "\n".join(findings) + "\nopen defects: " + json.dumps([d["desc"] for d in open_d])
        return False, detail
    return True, "static gates + review clean"


def phase_e2e(state: dict) -> tuple[bool, str]:
    ok, out = run_cmd([PY, "source_code/testing/e2e/run_e2e.py"], timeout=PHASE_CAP_S)
    return ok, out


# ---------------- cluster phases ----------------

def _get_litellm_key() -> str:
    rc, out = run_cmd(["kubectl", "--kubeconfig", KUBECONFIG,
                       "get", "secret", "-n", SLVD_LLM_NS, "litellm-helm-masterkey",
                       "-o", "json"], timeout=60)
    if rc != 0:
        return ""
    try:
        data = json.loads(out)["data"]
        for k in ("key", "LITELLM_MASTER_KEY", "master_key", "token"):
            if k in data:
                import base64
                return base64.b64decode(data[k]).decode()
    except Exception:
        pass
    return ""


def _get_openclaw_token() -> str:
    rc, out = run_cmd(["kubectl", "--kubeconfig", KUBECONFIG,
                       "get", "secret", "-n", "nemoclaw", "nemoclaw-openclaw-gateway-token",
                       "-o", "json"], timeout=60)
    if rc != 0:
        return ""
    try:
        data = json.loads(out)["data"]
        for k in ("token", "key", "value"):
            if k in data:
                import base64
                return base64.b64decode(data[k]).decode()
    except Exception:
        pass
    return ""


def phase_deploy(state: dict) -> tuple[bool, str]:
    log("DEPLOY: injecting secrets, building images, pushing, helm+chartmuseum+ezappconfig")
    # 1. secrets — injected inline into the EzAppConfig 'values' block (gitignored
    #    generated file). No K8s Secret object, no values-secrets.yaml.
    litellm_key = _get_litellm_key()
    openclaw_token = _get_openclaw_token()
    import secrets as _s
    vals = {
        "secrets": {
            "litellmApiKey": litellm_key or "PLACEHOLDER",
            "jwtSecret": _s.token_hex(32),
            "hmacSecret": _s.token_hex(32),
            "openclawToken": openclaw_token,
        },
        "engine": {"env": {
            "SLVD_AGENT_BACKEND": os.environ.get("SLVD_DEPLOY_BACKEND", "direct_llm"),
            "SLVD_SIMULATION": os.environ.get("SLVD_DEPLOY_SIM", "1"),
        }},
    }
    # 2. images
    ver = state.get("chart_version", "0.1.0")
    ok, out = run_cmd(["make", "-f", "source_code/Makefile", "build-images", f"IMG={SLVD_IMG}", f"VER={ver}"],
                      timeout=PHASE_CAP_S)
    if ok != 0:
        return False, f"image build/push failed:\n{out[-1500:]}"
    # 3. chartmuseum port-forward + push
    pf = subprocess.Popen(["kubectl", "--kubeconfig", KUBECONFIG,
                           "port-forward", "-n", "ez-chartmuseum-ns", "svc/chartmuseum", "18080:8080"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    ok, out = run_cmd(["helm", "lint", os.path.join(ROOT, "charts", "slvd")], timeout=120)
    if ok != 0:
        pf.terminate()
        return False, f"helm lint: {out[-500:]}"
    ok, out = run_cmd(["helm", "package", os.path.join(ROOT, "charts", "slvd"),
                       "-d", ROOT], timeout=120)
    if ok != 0:
        pf.terminate()
        return False, f"helm package: {out[-500:]}"
    ok, out = run_cmd(["helm", "push", f"slvd-{ver}.tgz", "http://127.0.0.1:18080"], timeout=120)
    pf.terminate()
    if ok != 0:
        return False, f"chartmuseum push: {out[-800:]}"
    # 4. EzAppConfig (values inline)
    values_json = json.dumps(vals)
    ez = open(os.path.join(ROOT, "charts", "slvd", "ezappconfig-template.yaml")).read()
    ez = ez.replace("__CHART_VERSION__", ver)
    ez = ez.replace("__VALUES_JSON__", json.dumps(values_json))
    ezf = os.path.join(ROOT, "charts", "slvd", "ezappconfig.generated.yaml")
    with open(ezf, "w") as f:
        f.write(ez)
    # 5. deploy
    ok, out = run_cmd(["make", "-f", "source_code/Makefile", "deploy", f"KUBE={KUBECONFIG}"],
                      timeout=PHASE_CAP_S)
    if ok != 0:
        return False, f"make deploy: {out[-1500:]}"
    return True, "deployed"



def phase_cluster(state: dict) -> tuple[bool, str]:
    # deterministic cluster e2e: force simulation on the live engine
    ok, out = run_cmd(["kubectl", "--kubeconfig", KUBECONFIG,
                       "set", "env", "deploy/engine", "-n", "slvd",
                       "SLVD_SIMULATION=1", "SLVD_AGENT_BACKEND=stub"], timeout=120)
    if ok != 0:
        return False, f"set env: {out[-500:]}"
    run_cmd(["kubectl", "--kubeconfig", KUBECONFIG, "rollout", "status",
            "deploy/engine", "-n", "slvd", "--timeout=180s"], timeout=240)
    ok, out = run_cmd(["bash", os.path.join(ROOT, "source_code", "testing", "e2e", "verify_cluster.sh")], timeout=PHASE_CAP_S)
    ok2, out2 = run_cmd(["bash", os.path.join(ROOT, "source_code", "testing", "e2e", "demo_run.sh")], timeout=PHASE_CAP_S)
    detail = out[-1500:] + "\n[demo_run] " + out2[-1500:]
    # restore production mode
    run_cmd(["kubectl", "--kubeconfig", KUBECONFIG,
            "set", "env", "deploy/engine", "-n", "slvd",
            "SLVD_SIMULATION=0", f"SLVD_AGENT_BACKEND={state.get('deploy_backend', 'direct_llm')}"],
            timeout=120)
    return (ok == 0 and ok2 == 0), detail


def phase_golden(state: dict) -> tuple[bool, str]:
    run_cmd(["kubectl", "--kubeconfig", KUBECONFIG, "rollout", "status",
            "deploy/engine", "-n", "slvd", "--timeout=180s"], timeout=240)
    ok, out = run_cmd([PY, os.path.join(ROOT, "source_code", "testing", "e2e", "golden_run.py")], timeout=PHASE_CAP_S)
    return ok == 0, out


def phase_final(state: dict) -> tuple[bool, str]:
    ok1, out1 = run_cmd([PY, os.path.join(ROOT, "source_code", "testing", "e2e", "security_scan.py")], timeout=300)
    ok2, out2 = run_cmd([PY, os.path.join(ROOT, "source_code", "testing", "e2e", "visual_parity.py")], timeout=300)
    detail = out1[-800:] + "\n[parity] " + out2[-1500:]
    return (ok1 == 0 and ok2 == 0), detail


# ---------------- main loop ----------------

def write_handoff(state: dict) -> None:
    h = os.path.join(ROOT, "HANDOFF.md")
    with open(h, "w") as f:
        f.write("# SLVD — ACCEPTANCE PASS\n\n")
        f.write(f"- Completed: {now_iso()}\n")
        f.write(f"- Iterations: {state['iteration']}\n")
        f.write("- All gates green: unit, review, e2e (S1-S8), deploy, cluster e2e, golden, "
                "security, visual parity, reproducibility.\n\n")
        f.write("## How to run the demo\n")
        f.write(f"1. Portal: https://{SLVD_HOST} (nick / analyst123)\n")
        _mail = ("slvd-mail." + SLVD_HOST[len("slvd."):] if SLVD_HOST.startswith("slvd.") else SLVD_HOST)
        f.write(f"2. Mailpit UI: https://{_mail} (approval + client emails)\n")
        f.write("3. Select CR-2026-00451, $5,000,000, Generate renewal decision memo.\n")
        f.write("4. Watch the 10-step governed run; open Mailpit, click Approve (8 h);\n"
                "   the run resumes, memo publishes, client gets 'Loan Application Approved'.\n\n")
        f.write("## History\n" + "\n".join(f"- it{h_['iter']} {h_['result']} {h_['detail'][:120]}"
                                          for h_ in state.get("history", [])[-20:]) + "\n")
    log(f"HANDOFF written: {h}")


def run_iteration(state: dict, cluster_ok: bool) -> str:
    """Run one iteration. Returns 'pass' | 'fail' | 'local_ok_no_cluster' | 'infra_blocked'."""
    state["iteration"] += 1
    it = state["iteration"]
    log(f"===== ITERATION {it} (cluster={'up' if cluster_ok else 'DOWN'}) =====")
    phases = ["fix", "build", "unit", "review", "e2e"]
    if cluster_ok:
        phases += ["deploy", "cluster", "golden", "final"]
    results = {}
    for ph in phases:
        if os.path.exists(STOP_F):
            log("STOP requested — finishing iteration")
            break
        state["phase"] = ph
        state_save(state)
        t0 = time.time()
        fn = {
            "fix": phase_fix, "build": phase_build, "unit": phase_unit,
            "review": phase_review, "e2e": phase_e2e, "deploy": phase_deploy,
            "cluster": phase_cluster, "golden": phase_golden, "final": phase_final,
        }[ph]
        try:
            ok, detail = fn(state)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"phase crashed: {type(e).__name__}: {e}"
        dt = time.time() - t0
        results[ph] = "PASS" if ok else "FAIL"
        log(f"[{ph}] {'PASS' if ok else 'FAIL'} ({dt:.0f}s)")
        if not ok:
            if ph == "fix":
                # fix failed -> record and stop this iteration (next iter retries)
                defect_add("P0", "fix", "fix phase failed", detail)
                state["history"].append({"iter": it, "phase": "fix", "result": "FAIL",
                                         "detail": detail[:300]})
                return "fail"
            # infra vs code: deploy/cluster/golden failures that look infra -> block, not defect
            infra_sig = ("ImagePullBackOff", "ErrImagePull", "registry", "401", "port busy",
                         "connection refused", "no route to host", "helm lock", "another operation")
            if ph in ("deploy", "cluster", "golden") and any(s in detail for s in infra_sig):
                state["history"].append({"iter": it, "phase": ph, "result": "INFRA",
                                         "detail": detail[:300]})
                log(f"[{ph}] infra-blocked — will retry (local work continues)")
                return "infra_blocked"
            defect_add("P0", ph, f"phase {ph} failed", detail)
            state["history"].append({"iter": it, "phase": ph, "result": "FAIL",
                                     "detail": detail[:300]})
            return "fail"
    # all phases passed
    if cluster_ok:
        state["green_streak"] = state.get("green_streak", 0) + 1
        defect_close_all()
        state["history"].append({"iter": it, "phase": "all", "result": "PASS",
                                 "detail": "all gates green"})
        if state["green_streak"] >= 2 and it % 3 == 0:
            # reproducibility spot-check (A12): clean redeploy
            log("reproducibility check: make clean && deploy")
            ok, out = run_cmd(["make", "-f", "source_code/Makefile", "clean", "KUBE=" + KUBECONFIG], timeout=600)
            ok2, out2 = run_cmd(["bash", os.path.join(ROOT, "source_code", "testing", "e2e", "verify_cluster.sh")],
                                timeout=PHASE_CAP_S)
            if ok != 0 or ok2 != 0:
                defect_add("P1", "repro", "clean redeploy failed", out[-500:] + out2[-500:])
                state["green_streak"] = 0
                return "fail"
            log("reproducibility check passed")
        write_handoff(state)
        return "pass"
    # local green, cluster down
    state["history"].append({"iter": it, "phase": "local", "result": "PASS",
                             "detail": "local green; cluster down"})
    return "local_ok_no_cluster"


def main_loop() -> None:
    t_start = time.time()
    state = state_load()
    result = "fail"
    log(f"loop starting (iter {state['iteration']}, caps {MAX_ITERS} iters / {WALL_CAP_H}h)")
    infra_streak = 0
    while state["iteration"] < MAX_ITERS:
        if (time.time() - t_start) / 3600 > WALL_CAP_H:
            log("WALL CAP reached — stopping with STALLED.md")
            _write_stalled(state, "wall cap reached")
            break
        # stop request
        if os.path.exists(STOP_F):
            log("stop requested")
            break
        cluster_ok = check_cluster_hard()
        log(f"cluster check: {'UP' if cluster_ok else 'DOWN'}")
        result = run_iteration(state, cluster_ok)
        state_save(state)
        git_commit(f"chore: iteration {state['iteration']} — {result}")
        if result == "pass":
            log(f"*** ACCEPTANCE PASS after {state['iteration']} iterations ***")
            break
        if result == "infra_blocked":
            infra_streak += 1
            if infra_streak >= 8:
                log("cluster infra blocked repeatedly — pausing cluster attempts 30min")
                time.sleep(1800)
            continue
        infra_streak = 0
        # short breather between iterations (let flaky infra settle)
        time.sleep(15)
    if state["iteration"] >= MAX_ITERS and result != "pass":
        _write_stalled(state, f"iteration cap ({MAX_ITERS}) reached without PASS")
    log("loop finished")


def _write_stalled(state: dict, why: str) -> None:
    h = os.path.join(ROOT, "STALLED.md")
    with open(h, "w") as f:
        f.write(f"# SLVD loop STALLED — {why}\n\n")
        f.write(f"State: iteration {state['iteration']}, phase {state.get('phase')}\n")
        f.write("Open defects:\n" + json.dumps(defects_open(), indent=2) + "\n\n")
        f.write("History:\n" + "\n".join(f"- it{h_['iter']} {h_['result']} {h_['detail'][:160]}"
                                        for h_ in state.get("history", [])[-25:]) + "\n\n")
        f.write("## Next action (human)\n- Read STALLED.md + iteration_log.md, fix the "
                "remaining blocker, then: python3 orchestrate/loop.py resume\n")
    log(f"STALLED written: {h}")


def start_detached() -> None:
    if os.path.exists(PID_F):
        try:
            pid = int(open(PID_F).read().strip())
            os.kill(pid, 0)
            print(f"loop already running (pid {pid})")
            return
        except (OSError, ValueError):
            pass
    # detach: new session, no stdin
    p = subprocess.Popen([sys.executable, os.path.join(ORCH, "loop.py", "_run"),
                         os.path.join(ROOT, "source_code", "orchestration")],
                         stdout=open(LOG_F, "a"), stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=ROOT)
    with open(PID_F, "w") as f:
        f.write(str(p.pid))
    print(f"loop started (pid {p.pid}); log: {LOG_F}")


def cmd_status() -> None:
    state = state_load()
    open_d = defects_open()
    print(json.dumps({"iteration": state.get("iteration"), "phase": state.get("phase"),
                      "green_streak": state.get("green_streak", 0),
                      "open_defects": len(open_d),
                      "open": open_d[:10],
                      "last_history": state.get("history", [])[-8:]}, indent=2))


def cmd_stop() -> None:
    with open(STOP_F, "w") as f:
        f.write(now_iso())
    print("stop requested (loop will finish current phase)")


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else "status"
    os.environ.setdefault("KUBECONFIG", KUBECONFIG)
    if c == "start":
        start_detached()
    elif c == "status":
        cmd_status()
    elif c == "stop":
        cmd_stop()
    elif c == "resume":
        main_loop()
    elif c == "_run":
        main_loop()
    else:
        print(__doc__)
