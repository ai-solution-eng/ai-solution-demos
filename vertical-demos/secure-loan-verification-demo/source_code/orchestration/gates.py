"""Blast-radius guardrails for the autonomous loop (plan §11.5).

Every kubectl WRITE command issued by the loop passes through kubectl_guarded().
Only these targets are allowed:
  - ns slvd (all resources)
  - ns ui   (EzAppConfig named slvd* only)
  - ns nemoclaw (ConfigMap openclaw config patch only — additive MCP registration)
  - ns istio-system (VirtualService named slvd* only)
Anything else is REJECTED (raises GuardError) so the loop can self-correct.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

KUBECONFIG = os.environ.get("SLVD_KUBECONFIG", "/home/ml/projects/kubeconfig-cs1.conf")

ALLOWED = {
    # ns: allowed resource kinds (None = all)
    "slvd": None,
    "ui": {"ezappconfig", "ezappconfigs.ezconfig.hpe.ezaf.com"},
    "nemoclaw": {"configmap"},
    "istio-system": {"virtualservice"},
    # read-only anywhere
}

READ_ONLY_VERBS = {"get", "describe", "logs", "exec", "port-forward", "top", "events", "explain"}


class GuardError(RuntimeError):
    pass


@dataclass
class Guard:
    cmd: list[str]

    def parse(self):
        # kubectl [<verb>] [resource] [name] [-n|--namespace ns] [-f file] [...]
        c = list(self.cmd)
        verb = None
        resource = None
        name = None
        ns = None
        files: list[str] = []
        i = 0
        while i < len(c):
            t = c[i]
            if t in ("kubectl", "k"):
                i += 1
                continue
            if t in ("--kubeconfig",):
                i += 2
                continue
            if t in ("-n", "--namespace"):
                ns = c[i + 1]
                i += 2
                continue
            if t in ("-f", "--filename"):
                files.append(c[i + 1])
                i += 2
                continue
            if t.startswith("--"):
                i += 2 if t in ("--for",) else 1
                continue
            if verb is None:
                verb = t
            elif resource is None:
                resource = t
            elif name is None:
                name = t
            i += 1
        return verb or "", resource or "", name or "", ns or "", files


ROOT_FOR_FILES = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root


def _norm_resource(res: str) -> str:
    r = (res or "").split(".")[0].lower()
    # kubectl singular abbreviations
    if r in ("ns",):
        return "namespace"
    if r in ("vs", "virtualservice"):
        return "virtualservice"
    if r == "pod":
        return "pod"
    if r.endswith("s"):
        return r.rstrip("s")
    return r


def check(cmd: list[str]) -> None:
    verb, resource, name, ns, files = Guard(cmd).parse()
    verb = verb.lower()
    if verb in READ_ONLY_VERBS:
        return  # reads are always fine
    res = _norm_resource(resource)
    # `kubectl delete ns slvd` / `create namespace slvd` — no -n flag, so check by name
    if res == "namespace":
        if name not in ("slvd",):
            raise GuardError(f"namespace '{name}' is not ours (only 'slvd')")
        return
    # `kubectl run <probe> -n slvd ...` — probe pods in our ns only
    if verb == "run":
        if ns != "slvd":
            raise GuardError(f"kubectl run in ns '{ns}' not allowed (only slvd)")
        return
    # file-based apply/delete: only files inside the project
    if files:
        for f in files:
            fp = os.path.abspath(f)
            if not fp.startswith(ROOT_FOR_FILES + os.sep):
                raise GuardError(f"file outside project: {f}")
        if ns:
            if ns not in ALLOWED:
                raise GuardError(f"namespace '{ns}' not in allowlist (verb={verb})")
            return
        # file defines its own namespaces — must be only slvd / ui / istio-system
        for f in files:
            txt = open(os.path.abspath(f), encoding="utf-8", errors="replace").read()
            for ns_in_file in re.findall(r"(?m)^[ \t]*namespace:[ \t]*(\S+)", txt):
                if ns_in_file not in ("slvd", "ui", "istio-system"):
                    raise GuardError(f"file references foreign namespace: {ns_in_file}")
        return
    if ns not in ALLOWED:
        raise GuardError(f"namespace '{ns}' not in allowlist (verb={verb})")
    allowed_kinds = ALLOWED[ns]
    if allowed_kinds is not None and res not in {k.split(".")[0].lower() for k in allowed_kinds}:
        raise GuardError(f"resource '{resource}' not allowed in ns '{ns}'")
    # ns ui: only slvd* EzAppConfigs
    if ns == "ui" and res in ("ezappconfig", "ezappconfigs"):
        if name and not name.startswith("slvd"):
            raise GuardError(f"ui/EzAppConfig '{name}' is not ours (must start with 'slvd')")
    # istio-system: only slvd* virtualservices
    if ns == "istio-system" and res == "virtualservice":
        if name and not name.startswith("slvd"):
            raise GuardError(f"istio-system/VirtualService '{name}' is not ours")
    # nemoclaw: only the openclaw configmap, and only apply/create (patch) — no delete
    if ns == "nemoclaw" and res == "configmap":
        if verb in ("delete",):
            raise GuardError("nemoclaw ConfigMap delete is not allowed (rollback handled by re-apply)")
        if name and "openclaw" not in name and "nemoclaw" not in name:
            raise GuardError(f"nemoclaw ConfigMap '{name}' is not the openclaw config")
    # generic: never delete/replace in ui or istio-system beyond slvd* (covered above)
    if verb in ("delete",) and ns in ("ui", "istio-system") and name and not name.startswith("slvd"):
        raise GuardError(f"delete of foreign resource {ns}/{name} blocked")


def kubectl(cmd_args: list[str], timeout: int = 300, check_output: bool = True) -> str:
    """Run a guarded kubectl command. cmd_args excludes the leading 'kubectl'."""
    check(["kubectl"] + list(cmd_args))
    full = ["kubectl", "--kubeconfig", KUBECONFIG, *cmd_args]
    r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    if check_output and r.returncode != 0:
        raise RuntimeError(f"kubectl failed ({r.returncode}): {' '.join(cmd_args)[:200]}\n{r.stderr[-500:]}")
    return r.stdout
