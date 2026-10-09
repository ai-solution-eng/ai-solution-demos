# values-examples — paste-ready values documents (secret-free)

This folder ships **sanitized, complete example values documents** for this chart. It is
mirrored by the hardlinker into the public delivery repo (GitHub), so it must stay
**intentionally secret-free**: no tokens, no platform JWTs, no real API keys. Credentials
are always role-named `<PLACEHOLDER>` values (see *Placeholder convention* below).

**Secrets mechanism (2026-09, P0-7):** the chart ships NO key material — `security.apiKey`
and `security.mediaTokenSecret` are sourced from the release Secret (auto-generated per
install, reused on upgrade) or from a Secret you own via `security.existingSecret`.
Rotation is OPTIONAL and operator-initiated; the runbook (every former location, the
replacement recipes, the "old keys stop working" warning) is `helm/ROTATION.md`.

**Do this FIRST (both profiles) — the one required secret decision:** point
`security.existingSecret` at a Secret you own carrying BOTH `RAG_API_KEY` and
`MEDIA_TOKEN_SECRET` (the containers refuse to start without the media secret):

```bash
kubectl -n <ns> create secret generic rag-platform-keys \
  --from-literal=RAG_API_KEY="$(openssl rand -hex 16)" \
  --from-literal=MEDIA_TOKEN_SECRET="$(openssl rand -hex 32)"
```

`values.g2.yaml` wires exactly that (`existingSecret: rag-platform-keys`);
`values.hosted-trial.yaml` shows the inline-filler alternative (or leave both empty and
let the chart auto-generate into `<deployment.name>-model-keys` on first install).
Everything else in the files is optional tuning. Since D20 (2026-09-24) a deployment with
no key configured anywhere is fail-closed (anonymous callers reach only the memory
dataset); any configured key restores normal access. The other Secret touchpoints —
`mcp.apiKey.existingSecret` (MCP-only keys) and `modelSecrets.*ApiKey` (model tokens →
the chart's model-keys Secret) — are marked `# SITE:` in both files.

## Placeholder convention

Every value **you** must replace is wrapped in angle brackets and named for
its role — `<MINIO_PASSWORD>`, `<EMBEDDER_API_KEY>`, `<USERNAME>`. The one
exception is `${DOMAIN_NAME}`: PCAI substitutes it before rendering, so leave
it as-is. Lowercase `<tokens>` inside comments are illustrative patterns, not
values.

## Files

| File | Target |
|---|---|
| `values.g2.yaml` | **SE G2** — HPE internal cluster (`pcai-se-ai-application.hst.rdlabs.hpecorp.net`, `project-user-*` namespaces, HPE SSO at the gateway). Model URLs are the real shared serving endpoints (not secret); every credential is a filler. |
| `values.hosted-trial.yaml` | **Hosted trial** — customer PCAI. Uses the `${DOMAIN_NAME}` placeholder (PCAI substitutes it before rendering), oauth2-proxy AuthorizationPolicy, and fillers for all model keys. |
| `values-hardened-g2.yaml` | **Hardened G2 overlay** — NOT a standalone profile: a fragment to merge into (or paste below) a profile file that turns the MCP network zone ON (`networkPolicy.enabled: true`). Base chart only — the scale charts ship no NetworkPolicy template. |

Both profile files also carry a commented-out `security.oidc` block (decision D21 — OIDC JWT as a second registry credential) at the same spot as the chart's `security` block: off by default, with the site-specific notes in the comments.

The two profile files are **complete paste-ready values documents** — a full copy of this chart's
`values.yaml` (not an override snippet) with site-specific lines marked `# SITE:`.
Each file's header states which chart variant it belongs to and when to choose that variant.
`values-hardened-g2.yaml` is the exception: an overlay fragment (see its header).

## Using them on PCAI

1. Import the packaged chart into PCAI once (PCAI users never run `helm install`).
2. Open the release's **Helm Values** editor and paste the whole example file in.
3. Adjust the lines marked `# SITE:` (endpoints, sizes, credentials).
4. Apply. PCAI substitutes `${DOMAIN_NAME}` before rendering.

Operators with cluster access can render-check first, without installing:

```bash
helm template <release> <chart-dir> -f <chart-dir>/values-examples/values.hosted-trial.yaml
```

## Where the real values live

Real per-site values (live credentials, per-customer endpoints) are kept in
`helm*/local/` — for this repo that directory is **gitignored, hardlink-ignored, and
excluded from packaged charts** (`.helmignore`). Never copy real credentials into this
folder or any tracked file; never commit them. See `helm/local/README.md` for the
convention.
