# Secret handling & rotation runbook — MultimodalRAG helm charts

Applies to all three chart variants: `helm/`, `helm-scale-medium/`, `helm-scale-large/`.

> **Status (2026-09, operator decision):** the values that were committed in these charts
> are **private — NOT considered burned — and no rotation is performed or required by the
> P0-7 change.** The fix HIDES them: no key material ships in the chart defaults, the
> delivery path, or the examples any more. Rotation remains available as an *optional,
> operator-initiated* procedure (§5) — read the D1 warning there before ever running it.

---

## 1 · What changed (P0-7, 2026-09)

Before: `security.apiKey` (REST key) and `security.mediaTokenSecret` (media-token HMAC
key) carried literal values in every variant's `values.yaml`, so every render of
`templates/secret.yaml` stamped those literals into the `*-model-keys` Secret, and every
repo reader / mirror had them.

After — the chart ships **no key material**. `templates/secret.yaml` sources the two keys
by precedence:

1. **`security.existingSecret`** — a Secret *you* own provides both keys (key names:
   `security.existingSecretApiKey` / `security.existingSecretMediaTokenKey`, defaults
   `RAG_API_KEY` / `MEDIA_TOKEN_SECRET`). The chart then renders **neither** key into its
   own Secret, and `templates/deployment.yaml` wires both env vars from your Secret via
   `secretKeyRef` (both containers).
2. **Inline values** `security.apiKey` / `security.mediaTokenSecret` — kept for
   **back-compat**: an existing values file that sets them behaves exactly as before.
3. **Lookup reuse** — `lookup "v1" "Secret" .Release.Namespace <deployment.name>-model-keys`:
   on `helm upgrade`, the values written by the previous install are reused, so upgrading
   **never rotates a secret underneath a running deployment**.
4. **Fresh generation** on first install (nothing found): REST key = `randAlphaNum 32`,
   media-token secret = 64 hex (`randAlphaNum 32 | sha256sum`). `helm install` therefore
   still works with **zero new user steps**.

Notes:
- The app reads both keys from the **environment only** (`os.environ`), so omitting them
  from the chart Secret in mode 1 is safe.
- Leaving `mediaTokenSecret` empty with generation disabled would abort startup — the app
  refuses to run without it (`api_server.py` exits 1) — which is why mode 3/4 exist.
- To bring your own key material without `existingSecret`: pre-create the chart's own
  Secret name before install (lookup will reuse it), or just use `existingSecret` with a
  distinct Secret name (cleaner ownership).

Retrieve an auto-generated value:

```bash
kubectl -n <ns> get secret <deployment.name>-model-keys \
  -o jsonpath='{.data.RAG_API_KEY}' | base64 -d; echo
kubectl -n <ns> get secret <deployment.name>-model-keys \
  -o jsonpath='{.data.MEDIA_TOKEN_SECRET}' | base64 -d; echo
```

---

## 2 · Every place the literals lived (locations — values not reprinted)

The literals are identified by fingerprint (`first5…last4`). Full values remain in the
private MultimodalRAG git history (`HEAD 326f85f` and earlier); history rewrite is out of
scope for P0-7 (rotation-not-scrub policy; repo is private).

### Chart defaults (tracked, delivery-visible) — REMOVED

| Location | Keys | Fingerprint | Now |
|---|---|---|---|
| `MultimodalRAG/helm/values.yaml` (was lines 105-106) | `security.apiKey`, `security.mediaTokenSecret` | `Zst_q…2UnG`, `48b00…944d` | `''` + mechanism comments |
| `MultimodalRAG/helm-scale-large/values.yaml` (was 142-143) | same keys, same values | same | same |
| `MultimodalRAG/helm-scale-medium/values.yaml` (was 132-133) | same keys, same values | same | same |

### Tracked example values — PLACEHOLDERS

| Location | Was | Now |
|---|---|---|
| `helm/local/values.example.yaml` (was 181/187) | the leaked chart-default values | `CHANGE-ME-…` placeholders + mechanism pointer |
| `helm-scale-large/local/values.example.yaml` (was 295/301) | same | same |
| `helm-scale-medium/local/values.example.yaml` (was 279/285) | same | same |
| `helm*/values-examples/values.g2.yaml`, `values.hosted-trial.yaml` (×2 variants) | already `<RAG_API_KEY>` / `<MEDIA_TOKEN_SECRET>` fillers | unchanged fillers + mechanism/`ROTATION.md` pointer + new `existingSecret` keys |

### Delivery mirrors (pcai-solutions — synced by the orchestrator, not edited by agents)

| Location | Keys | Fingerprint | Disposition |
|---|---|---|---|
| `pcai-solutions/ai-solution-demos/multimodal-rag/helm/values.yaml` (was 165/175) | `security.apiKey`, `security.mediaTokenSecret` | `Zst_q…2UnG`, `48b00…944d` | re-synced from the source chart → same hiding lands there; **re-grep after sync** |
| `pcai-solutions/foundational-workflows/multimodal-rag/helm/values.yaml` (was 105-106) | same | same | same |

Verify after the orchestrator's sync:

```bash
grep -rn "Zst_qoSr\|48b00cdf" pcai-solutions/ | grep -v History   # expect: no output
```

### Other app (recorded here, owned elsewhere)

| Location | Key | Fingerprint | Disposition |
|---|---|---|---|
| `mcp_servers/searxng_mcp/helm/local/values.g2.yaml:57` (renamed from values.se-g2.yaml, 2026-09-18 cleanup) | `secretKey` (SearXNG site secret) | `ff3a87…b95d` | A3 replaces with placeholder/`existingSecret` in the same wave; file is gitignored |

### Retained private values (UNTOUCHED by design — operator decision, gitignored)

These are gitignored (`helm*/local/*`), never mirrored, never packaged (`.helmignore`);
the operator keeps the working values in place — **replacing them would have forced a
rotation, which was explicitly not wanted**:

| Location | Keys | Fingerprint |
|---|---|---|
| `helm-scale-large/local/values.g2.yaml` (ex-se_g2.yaml; deleted 2026-09-18 — merged into values.g2.yaml) | `security.apiKey`, `security.mediaTokenSecret` | `_55_V…PoPd`, `5787d…a7dd` |
| `helm-scale-large/local/values.g2.yaml` (57-60, 144-145) | `modelSecrets.*ApiKey` (4 model-serving **JWTs**), `security.apiKey`, `security.mediaTokenSecret` | JWTs (`eyJhbG…`), `_55_V…PoPd`, `5787d…a7dd` |
| `helm-scale-large/local/values.omnilife.yaml` (ex-omnilife.yaml, restored 2026-09-18) (88-104, 215-216) | `modelSecrets` JWTs, `s3.accessKeyId`/`s3.secretAccessKey` (MinIO), `security.apiKey`/`mediaTokenSecret` | JWTs, `iZEud…Cbu6`, `MYSwK…Qw81`, `Y4GlF…5M5g`, `ed735…06be` |
| `helm-scale-large/local/migrate-my-memory.py` (31) | `API_KEY` (G2 REST key baked into the helper script) | `_55_V…PoPd` |
| `MultimodalRAG/.g2_cluster.yaml` (100-103, 243; repo root) | `modelSecrets.*ApiKey` JWTs + `security.mediaTokenSecret` | JWTs (`eyJhbG…`), `5787d…a7dd` |
| SearXNG site file (recorded for A3/wave boundary) | `secretKey` | see §2 "Other app" |

Only a now-false comment in `se_g2.yaml` (since merged into `values.g2.yaml` and deleted; see 2026-09-18 cleanup) ("the charts ship the same default key") was
corrected — the values themselves were not touched. All of the above are gitignored /
untracked and never mirrored; `secret_scan.sh` flags them on a whole-tree scan by design
(they are the working credentials), which is the intended tripwire: if one of these ever
becomes tracked or mirrored, the scan goes red.

---

## 3 · Providing keys going forward (pick one)

| Path | When | How |
|---|---|---|
| Do nothing (recommended default) | fresh installs, zero steps | chart generates both keys into `<release>-model-keys`; retrieve per §1 |
| `security.existingSecret` | you manage keys centrally (multi-chart, PCAI) | `kubectl -n <ns> create secret generic <name> --from-literal=RAG_API_KEY=$(openssl rand -hex 16) --from-literal=MEDIA_TOKEN_SECRET=$(openssl rand -hex 32)` then `--set security.existingSecret=<name>` |
| Inline `security.apiKey` / `security.mediaTokenSecret` | back-compat with existing values files | exactly as before (works, but keep real values out of tracked files — use gitignored `local/`) |

Existing inline users: unaffected — precedence 2 keeps their values working verbatim.
Existing deployed clusters: upgrading does not change their Secret (precedence 3 reuses
the released values).

---

## 4 · Rolling a deployment after changing key *provisioning* (not rotating values)

```bash
helm upgrade <release> <chart-dir> -n <ns> --reuse-values [--set security.existingSecret=…]
kubectl -n <ns> rollout restart deploy/<deployment.name>   # only needed if you switched provisioning mode
```

The chart Secret itself is not a rollout trigger when its values don't change (lookup
reuse keeps renders stable); when switching from generated → `existingSecret`, restart so
both containers re-read env.

---

## 5 · OPTIONAL rotation procedure (only if ever needed) — decision D1

> **D1 warning:** after a rotation the **old keys stop working immediately** and **every
> already-issued media token is invalid** (short-lived HMAC tokens minted with the old
> `MEDIA_TOKEN_SECRET` — links embedded in older LLM replies / bookmarks break at once).
> This is a **one-time ops step the operator performs**, never an agent action. Dataset
> passwords are PBKDF2-hashed independently and are NOT affected; only the REST API key,
> MCP keys (separate `mcp.apiKey` mechanism in `helm/`, unchanged), and media tokens move.

1. **Mint replacements** (run on a trusted host, not in the repo):
   - REST API key: `openssl rand -hex 16` (or `python -c "import secrets; print(secrets.token_urlsafe(24))"`)
   - Media-token secret: `openssl rand -hex 32` (must be high-entropy; 64 hex is the convention)
   - SearXNG `secretKey` (if its site file is ever rotated): `openssl rand -hex 32`
2. **Stage into the Secret** (no downtime for the API key if you use the comma-list trick
   on MCP; REST key is single-valued — plan a brief client cutover):
   ```bash
   kubectl -n <ns> create secret generic <deployment.name>-model-keys \
     --from-literal=RAG_API_KEY=<new> --from-literal=MEDIA_TOKEN_SECRET=<new> \
     --dry-run=client -o yaml | kubectl apply -f -
   kubectl -n <ns> rollout restart deploy/<deployment.name>
   ```
   (Or set them inline via `--set` — same effect, worse hygiene.)
3. **Update clients** (REST callers send `X-RAG-Api-Key`/Bearer; Open WebUI valve
   `RAG_API_KEY`; anything holding media links re-fetches them).
4. **Verify**: `curl -H "X-RAG-Api-Key: <new>" .../api/datasets` → 200; an old key → 401.
5. **Mirror hygiene**: after any tracked-file change, the orchestrator re-syncs the
   `pcai-solutions` mirrors and re-greps them (§2 commands).

**Never** paste the new values into tracked files; gitignored `helm*/local/` or Secrets only.

---

## 6 · Guardrail: `pcai_utils/secret_scan.sh`

Standalone scanner (not part of the hardlink mesh) that would have caught P0-7:
64-hex literals, AWS key IDs, private-key headers, and high-entropy assignments to
secret/password/token/apiKey-named keys, across yaml/yml/py/md/txt/env files. Exit 1 on
any hit. Self-test: `bash pcai_utils/secret_scan.sh --self-test`.
Suggested CI/pre-commit wiring: `pcai_utils/hooks/pre-commit.secret-scan` (consumed by the
Wave-2 git agent).
