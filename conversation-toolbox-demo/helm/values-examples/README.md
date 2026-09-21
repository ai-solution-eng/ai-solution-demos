# values-examples — paste-ready, secret-free values for Conversation Toolbox

This folder holds **complete, paste-ready Helm values documents** for the deployment targets of the `conversation-toolbox` chart. Every file here is a *full* values document (not an override snippet) so it can be pasted as-is into PCAI's **Helm Values** editor, with only the `# SITE:`-marked lines and credentials left to adjust.

**This folder is hardlinked by `hardlinker.py` into the delivery repo (GitHub) — everything here must stay secret-free.** Never put real credentials, customer tokens, or private hostnames with embedded keys in these files.

Real per-site values live in [`helm/local/`](../local/README.md) — gitignored, hardlink-ignored, and excluded from chart packaging (`.helmignore`). Copy a file from here into `helm/local/values.<site>.yaml`, fill it in, `chmod 600`, and keep it there.

| File | Target |
|---|---|
| [`values.g2.yaml`](values.g2.yaml) | HPE SE G2 PCAI cluster (`pcai-se-ai-application.hst.rdlabs.hpecorp.net`), HPE proxy, `project-user-*` namespaces. Endpoint pre-filled with the G2 domain. |
| [`values.hosted-trial.yaml`](values.hosted-trial.yaml) | Customer-hosted PCAI trial: `${DOMAIN_NAME}` placeholders (PCAI resolves them), oauth2-proxy SSO, notes on the chart's Kyverno ClusterPolicy. |

## Using on PCAI (recommended)

1. Import the packaged chart (`conversation-toolbox-<version>.tar.gz`) into PCAI once.
2. Create the application from the chart and open the **Helm Values** editor.
3. Paste the full contents of the file for your target.
4. Adjust every `# SITE:` line (image mirror, model endpoints, storage class, GPU node…) and replace every `<ROLE_NAME>` placeholder.
5. Apply. Later changes = edit values → re-apply.

## Using as an operator (non-PCAI Kubernetes)

```bash
helm template conversation-toolbox ../ -f values.g2.yaml      # eyeball the render first
helm upgrade --install conversation-toolbox ../ -n <namespace> -f values.g2.yaml
```

## Placeholder convention

Every value **you** must replace is wrapped in angle brackets and named for
its role — `<MINIO_PASSWORD>`, `<EMBEDDER_API_KEY>`, `<USERNAME>`. The one
exception is `${DOMAIN_NAME}`: PCAI substitutes it before rendering, so leave
it as-is. Lowercase `<tokens>` inside comments are illustrative patterns, not
values.

## Secrets hygiene

- Credential values here are always `<ROLE_NAME>` placeholders — never real.
- `${DOMAIN_NAME}` is the one non-user placeholder: PCAI substitutes it before rendering.
- Preferred: don't put credentials in values at all — create the Secret out-of-band and set `credentialsSecret.create: false` (commented block in each file) so keys never pass through a values document.
- If the chart must create the Secret from values, those values live **only** in `helm/local/`.
