#!/usr/bin/env python3
"""fleet_reconcile.py — bidirectional reconcile for the pcai_utils hardlink mesh.

The mesh is one-directional by design (pcai_utils is the source of truth), but
REAL edits happen everywhere: DSH's editor replaces files atomically (new
inode, mesh detached), release waves rewrite consumer copies, and Syncthing
peers re-materialize mirror files. Left alone, the next `link_utils.sh` run
OVERWRITES that work with stale source content — the asymmetry bites.

This tool closes the loop by making drift DIRECTION-decidable instead of
guessing. For every managed file it compares the sha256 of the pcai_utils
source and every expected copy (repo + pcai-solutions mirror paths) against
the sha recorded at the last reconcile (`.fleet_reconcile_state.json`):

    copy changed, source at baseline   -> PROMOTE  (copy content -> source inode
                                                   via truncate-write; every
                                                   linked copy updates at once;
                                                   detached copies re-linked)
    source changed, copies at baseline -> REPAIR   (ln -f copies onto source)
    both moved to the SAME content     -> NORMALIZE (relink; nothing lost)
    both moved APART                   -> CONFLICT  (reported, never guessed —
                                                   exit 1, file untouched)

Mirrors get the identical treatment, so a peer-write reversion to a mirror
(rep unchanged) is repaired from the repo automatically, and a mirror is never
allowed to redefine fleet content — only a REPO copy can promote.

Everything is dry-run by default; `--run` applies. Scope with `--repos` (the
"directories of interest"): repo names exactly as passed to link_utils.sh
(e.g. `SQLhandler`, `mcp_servers/applygate_mcp`). `--build` additionally runs
the hardlinker mirror build for the scoped repos after reconciling, so one
command goes from edited tree to delivery-ready pcai-solutions.

Hardlink semantics preserved throughout: promote = truncate-write (inode
kept), everything else = `ln -f`. No temp-file-and-rename anywhere.

Exit codes: 0 in sync / reconciled clean, 1 conflicts or errors found,
2 usage. With no state file, the first run BOOTSTRAPS: it only records
baseline state for files that are already fully in sync (all copies ==
source) and reports any disagreement as a conflict — it never invents a
direction.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

HPE = Path("/home/andrew/Code/HPE")
PC = HPE / "pcai_utils"
STATE = PC / ".fleet_reconcile_state.json"
EXCEPTIONS = PC / "reconcile_exceptions.json"

# Managed root files (mirrors check_links.sh ROOT_FILES + formatter targets).
ROOT_FILES = ["hardlinker.py", "prune_charts.py", "ruff.toml", "mypy.ini", "bump_version.sh", "mcp_auth.py"]
# Control-plane scripts that STAY in pcai_utils — never distributed into
# consumer utils dirs (same class as hardlinker.py / prune_charts.py).
CONTROL_PLANE = {"hardlinker.py", "prune_charts.py", "fleet_reconcile.py"}
# pcai_utils modules distributed into src/*/utils (derived, like the gate).
UTIL_MODULES = [f for f in sorted(PC.glob("*.py")) if f.name not in CONTROL_PLANE and "sync-conflict" not in f.name]
PREPROCESSORS = sorted((PC / "preprocessors").glob("*.py"))


def sha_of(p: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()[:12]


def load_exceptions() -> dict:
    """Deliberate non-mesh copies (frozen vintages, vendored forks), keyed by
    HPE-relative path prefix: {"reason": "..."}. Reconcile skips any copy
    whose relative path equals or falls under a key."""
    if EXCEPTIONS.is_file():
        try:
            return json.loads(EXCEPTIONS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def is_exempt(rel: str, exc: dict) -> bool:
    return any(rel == k or rel.startswith(k.rstrip("/") + "/") for k in exc)


def git_modified(p: Path) -> bool | None:
    """True/False when *p* sits inside a git work tree and its tracked state
    is known; None when git cannot answer (no repo, untracked)."""
    top = p
    while top != top.parent:
        if (top / ".git").exists():
            break
        top = top.parent
    else:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(top), "status", "--short", "--", str(p.relative_to(top))],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return not out  # False = tracked and clean; True = modified/staged/untracked work


def target_repos() -> list[Path]:
    repos: list[Path] = []
    for cfg in glob.glob(str(HPE / "*" / "hardlink_config.json")) + glob.glob(
        str(HPE / "mcp_servers" / "*" / "hardlink_config.json")
    ):
        repos.append(Path(cfg).parent)
    for ab in (HPE / "Deprecated" / "AgentBuilder", HPE / "AgentBuilder"):
        if ab.is_dir():
            repos.append(ab)
            break
    return sorted(set(repos))


def has_chart(repo: Path) -> bool:
    return bool(glob.glob(str(repo / "helm*" / "Chart.yaml")))


def utils_dir(repo: Path) -> Path | None:
    for d in sorted(glob.glob(str(repo / "src" / "*"))):
        if (Path(d) / "utils").is_dir():
            return Path(d) / "utils"
    return None


def package_dir(repo: Path) -> Path | None:
    for d in sorted(glob.glob(str(repo / "src" / "*"))):
        if Path(d).is_dir():
            return Path(d)
    return None


def expected_copies(repo: Path) -> list[tuple[str, Path]]:
    """(label, path) for every copy of managed content this repo should hold.

    Mirrors check_links.sh expectations; files the repo legitimately lacks
    (no src utils, no chart, root-only layout) are simply absent — absent
    means "not managed here", never "deleted".
    """
    out: list[tuple[str, Path]] = []
    ud = utils_dir(repo)
    if ud:
        for f in UTIL_MODULES:
            out.append((f.name, ud / f.name))
        for f in PREPROCESSORS:
            out.append((f"preprocessors/{f.name}", ud / "preprocessors" / f.name))
    for name in ROOT_FILES:
        if name == "bump_version.sh" and not has_chart(repo):
            continue
        if name == "mcp_auth.py" and not (repo / name).exists():
            continue  # root mcp_auth only where the layout already has it
        out.append((name, repo / name))
    pkg = package_dir(repo)
    if pkg:
        out.append(("formatter.sh", pkg / "formatter.sh"))
    if (repo / "tests").is_dir():
        out.append(("formatter.sh", repo / "tests" / "formatter.sh"))
    return out


def mirror_copies(repo: Path) -> list[tuple[str, Path]]:
    """Mirror-path equivalents of this repo's managed copies (same rel path
    under the dest tree), via the repo's hardlink_config.json."""
    cfg_path = repo / "hardlink_config.json"
    if not cfg_path.is_file():
        return []
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    dest = cfg.get("dest")
    if not dest or not Path(dest).is_dir():
        return []
    dest = Path(dest)
    out = []
    for _, p in expected_copies(repo):
        rel = p.relative_to(repo)
        out.append((str(p.relative_to(HPE)), dest / rel))
    return out


def relink(src: Path, dst: Path) -> str:
    """(Re)engage dst as a hardlink of src. Returns an action label."""
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    os.link(src, dst)
    return "relinked"


def promote(src_repo_copy: Path, source: Path) -> str:
    """Copy content INTO the source inode (truncate-write — inode preserved,
    every existing link updates atomically), then relink the origin copy."""
    data = src_repo_copy.read_bytes()
    with open(source, "wb") as f:  # truncate-write: inode preserved
        f.write(data)
    os.chmod(source, 0o644)
    return "promoted"


class Reconciler:
    def __init__(self, run: bool, build: bool, verbose: bool, min_age: float = 0.0):
        self.run = run
        self.build = build
        self.verbose = verbose
        self.min_age = min_age  # seconds a repo copy must have been quiet
        self.build_anyway = False
        self.state = self._load_state()
        self.actions: list[str] = []
        self.conflicts: list[str] = []
        self.new_state: dict[str, str] = {}

    def _too_fresh(self, p: Path) -> bool:
        """True when the copy was written more recently than the stability
        window — an in-flight edit (a truncate-write is briefly partial).
        Automation passes --min-age; interactive runs default to 0."""
        if self.min_age <= 0:
            return False
        try:
            return (time.time() - p.stat().st_mtime) < self.min_age
        except OSError:
            return False

    def _load_state(self) -> dict:
        if STATE.is_file():
            try:
                return json.loads(STATE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    def _save_state(self) -> None:
        if self.run:
            tmp = STATE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, STATE)

    def log(self, msg: str) -> None:
        print(msg)
        self.actions.append(msg)

    # ------------------------------------------------------------------

    def reconcile_file(self, key: str, source: Path, copies: list[Path]) -> bool:
        """Reconcile one managed file across source + copies. Returns True
        when clean (synced or repaired), False on conflict."""
        if not source.is_file():
            return True  # nothing to manage (defensive)
        src_sha = sha_of(source)
        if src_sha is None:
            self.conflicts.append(f"  [ERROR] {key}: source unreadable ({source}) — nothing decided")
            return False
        base = self.state.get(key) or self.state.get(f"file:{key}")
        cur: list[tuple[Path, str | None]] = [(source, src_sha)]
        for c in copies:
            cur.append((c, sha_of(c) if c.exists() else None))
        # Destinations absent from disk (sha None because there was no file to
        # hash): created as fresh links by the PROMOTE / LINK-MISSING paths.
        missing = [p for p, s in cur if p != source and s is None]

        groups: dict[str, list[Path]] = defaultdict(list)
        for p, s in cur:
            groups[s or "<missing>"].append(p)

        # 1) Everything identical -> ensure hardlinked, record baseline.
        if len(groups) == 1 and None not in groups:
            changed = []
            for p in groups[src_sha]:
                if p != source and (not p.exists() or os.stat(source).st_ino != os.stat(p).st_ino):
                    changed.append(p)
            if changed:
                if self.run:
                    for p in changed:
                        relink(source, p)
                self.log(
                    f"  [NORMALIZE] {key}: {len(changed)} detached copy(ies) "
                    f"re-linked ({'ran' if self.run else 'dry-run'})"
                )
            self.new_state[key] = src_sha
            return True

        # 2) Bootstrap with no baseline: git is the direction arbiter.
        if base is None:
            divergent = [(p, s) for p, s in cur if p != source and s is not None and s != src_sha]
            if divergent:
                mods = {p: git_modified(p) for p, _ in divergent}
                fresh = [p for p, _ in divergent if self._too_fresh(p)]
                if fresh:
                    self.log(
                        f"  [SKIP-FRESH] {key}: copy written <{self.min_age:.0f}s ago "
                        f"— skipped this pass (in-flight edit guard)"
                    )
                    self.new_state[key] = src_sha
                    return True
                if all(m is True for _, m in mods.items()) and len(mods) == 1:
                    # Uncommitted repo work: the repo copy is the advancing
                    # side — promote it exactly as the steady-state path would.
                    winner = divergent[0][0]
                    if self.run:
                        promote(winner, source)
                        for p in [c for c in copies if c.exists()]:
                            if os.stat(source).st_ino != os.stat(p).st_ino:
                                relink(source, p)
                        for p in missing:
                            p.parent.mkdir(parents=True, exist_ok=True)
                            os.link(source, p)
                    self.log(
                        f"  [PROMOTE] {key}: {winner.relative_to(HPE)} -> source "
                        f"inode (bootstrap: uncommitted repo work wins) "
                        f"({'ran' if self.run else 'dry-run'})"
                    )
                    self.new_state[key] = sha_of(source) or src_sha
                    return True
                if all(m is False for m in mods.values()):
                    self.conflicts.append(
                        f"  [CONFLICT-BOOTSTRAP] {key}: divergent copy is git-COMMITTED "
                        f"divergence — manual merge required\n"
                        f"    source={src_sha}  " + "  ".join(f"{p.relative_to(HPE)}={s}" for p, s in divergent)
                    )
                    return False
            self.conflicts.append(
                f"  [CONFLICT-BOOTSTRAP] {key}: copies disagree with source and "
                f"no baseline exists — resolve by hand, then re-run\n"
                f"    source={src_sha}  "
                + "  ".join(f"{p.relative_to(HPE)}={s}" for p, s in cur if p != source and s != src_sha)
            )
            return False

        # 3) Direction-decidable paths.
        src_changed = src_sha != base
        repo_changed = [(p, s) for p, s in cur if p != source and s is not None and s != base]
        same_as_source = [p for p, s in repo_changed if s == src_sha]

        if src_changed and not repo_changed:
            if self.run:
                for p in [c for c in copies if c.exists()]:
                    if os.stat(source).st_ino != os.stat(p).st_ino:
                        relink(source, p)
            self.log(
                f"  [REPAIR] {key}: source advanced -> "
                f"{len(copies)} copy path(s) re-linked "
                f"({'ran' if self.run else 'dry-run'})"
            )
            self.new_state[key] = src_sha
            return True

        if not src_changed and repo_changed:
            fresh = [p for p, _ in repo_changed if self._too_fresh(p)]
            if fresh:
                self.log(
                    f"  [SKIP-FRESH] {key}: copy written <{self.min_age:.0f}s ago "
                    f"— skipped this pass (in-flight edit guard)"
                )
                self.new_state[key] = src_sha
                return True
            shas = {s for _, s in repo_changed}
            if len(shas) == 1:
                winner = repo_changed[0][0]
                if self.run:
                    promote(winner, source)
                    for p in [c for c in copies if c.exists()]:
                        if os.stat(source).st_ino != os.stat(p).st_ino:
                            relink(source, p)
                    for p in missing:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        os.link(source, p)
                self.log(
                    f"  [PROMOTE] {key}: {winner.relative_to(HPE)} -> source "
                    f"inode (fleet-wide); {len(repo_changed)} copy(ies) updated "
                    f"({'ran' if self.run else 'dry-run'})"
                )
                self.new_state[key] = sha_of(source) or src_sha
                return True
            self.conflicts.append(
                f"  [CONFLICT] {key}: multiple repos advanced differently — "
                f"manual merge required\n    " + "  ".join(f"{p.relative_to(HPE)}={s}" for p, s in repo_changed)
            )
            return False

        if src_changed and repo_changed:
            if len(same_as_source) == len(repo_changed):
                # Copies moved to exactly the source content: normalize.
                if self.run:
                    for p in [c for c in copies if c.exists()]:
                        if os.stat(source).st_ino != os.stat(p).st_ino:
                            relink(source, p)
                self.log(
                    f"  [NORMALIZE] {key}: copy content matches advanced source ({'ran' if self.run else 'dry-run'})"
                )
                self.new_state[key] = src_sha
                return True
            self.conflicts.append(
                f"  [CONFLICT] {key}: source AND repo(s) advanced apart — manual "
                f"merge required\n    source={src_sha} base={base}  "
                + "  ".join(f"{p.relative_to(HPE)}={s}" for p, s in repo_changed)
            )
            return False

        # missing copies only
        if missing and not src_changed and not repo_changed:
            if self.run:
                for p in missing:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    os.link(source, p)
            self.log(
                f"  [LINK-MISSING] {key}: {len(missing)} absent copy(ies) linked ({'ran' if self.run else 'dry-run'})"
            )
            self.new_state[key] = src_sha
            return True

        self.new_state[key] = src_sha
        return True

    # ------------------------------------------------------------------

    def run_fleet(self, repos: list[Path]) -> int:
        if not repos:
            repos = target_repos()
        exc = load_exceptions()
        bootstrap = not STATE.is_file()
        print(
            f"== fleet reconcile — {'BOOTSTRAP' if bootstrap else 'baseline-verified'}"
            f" — scope: {', '.join(r.name for r in repos) or 'ALL'} — "
            f"{'RUN' if self.run else 'DRY-RUN'} =="
        )
        ok = True
        for repo in repos:
            print(f"-- {repo.relative_to(HPE)}")
            pairs = [
                (f"{p.relative_to(HPE)}", source, [p]) for source, p in self._source_for(repo, expected_copies(repo))
            ]
            for key, source, copies in pairs:
                if is_exempt(key, exc):
                    continue
                ok &= self.reconcile_file(key, source, copies)
            # Mirror copies: the REPO copy is the content source (a mirror is
            # never allowed to redefine fleet content — only a repo can
            # promote). Direction logic still applies: if the mirror alone
            # moved (peer-write reversion case), the repo copy wins REPAIR.
            for rel, mp in mirror_copies(repo):
                if is_exempt(rel, exc):
                    continue
                if not mp.exists():
                    continue  # absent mirror file = not managed here
                rest = Path(*Path(rel).parts[1:])  # strip "<repoName>/"
                repo_copy = repo / rest
                if repo_copy.is_file():
                    ok &= self.reconcile_file(f"{rel} (mirror)", repo_copy, [mp])
        self.state.update({k: v for k, v in self.new_state.items()})
        self._save_state()
        if self.conflicts:
            print("\n== CONFLICTS (nothing written for these) ==")
            for c in self.conflicts:
                print(c)
        print(f"\n== reconcile: {len(self.actions)} action(s), {len(self.conflicts)} conflict(s) ==")
        # --build only fires when reconcile actually did something (plus the
        # standalone escape hatch --build-anyway).
        if self.build and self.run and not self.conflicts and (self.actions or self.build_anyway):
            self._build(repos)
        return 0 if ok else 1

    def _source_for(self, repo: Path, copies: list[tuple[str, Path]]):
        """Group copies by managed file -> (source_path, copy_path)."""
        for name, p in copies:
            if name.startswith("preprocessors/") or name in ROOT_FILES or name == "formatter.sh":
                yield PC / name, p
            else:
                yield PC / name, p

    def _build(self, repos: list[Path]) -> None:
        print("\n== build: hardlinker mirror run for scoped repos ==")
        for repo in repos:
            cfg = repo / "hardlink_config.json"
            if not cfg.is_file():
                continue
            r = subprocess.run(
                [sys.executable, str(PC / "hardlinker.py"), "-c", str(cfg), "--run"], capture_output=True, text=True
            )
            tail = [ln for ln in r.stdout.splitlines() if "Done." in ln or "Errors:" in ln]
            print(f"  {repo.name}: {' '.join(tail[-1:]) or r.stdout.strip()[-80:]}")
            if r.returncode != 0:
                self.conflicts.append(f"  [BUILD-ERROR] {repo.name}: {r.stderr[-200:]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", action="store_true", help="apply actions (default: dry-run)")
    ap.add_argument("--repos", nargs="*", default=None, help="scope to these repo roots (names as for link_utils.sh)")
    ap.add_argument(
        "--build",
        action="store_true",
        help="after reconcile (when it acted), run the hardlinker mirror build for the scoped repos",
    )
    ap.add_argument(
        "--build-anyway",
        action="store_true",
        help="after reconcile, run the hardlinker mirror build for "
        "the scoped repos (delivery trees ready for git/github)",
    )
    ap.add_argument(
        "--min-age",
        type=float,
        default=0.0,
        help="stability window: skip promoting copies written more recently than this many seconds (automation guard)",
    )
    ap.add_argument(
        "--reset-baseline", action="store_true", help="forget the recorded baseline (next run re-bootstraps)"
    )
    args = ap.parse_args()

    if args.reset_baseline and STATE.is_file():
        STATE.unlink()
        print("baseline reset")

    repos = [Path(r) if Path(r).is_absolute() else HPE / r for r in (args.repos or [])]
    for r in repos:
        if not r.is_dir():
            print(f"ERROR: no such repo root: {r}", file=sys.stderr)
            return 2
    rc = Reconciler(run=args.run, build=args.build, verbose=True, min_age=args.min_age)
    rc.build_anyway = args.build_anyway
    return rc.run_fleet(repos)


if __name__ == "__main__":
    sys.exit(main())
