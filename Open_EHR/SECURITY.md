# Security Policy

OpenCare EHR models clinical workflows. Security reports are taken
seriously and handled discreetly.

## Reporting a vulnerability

Do **not** open a public issue for a security problem. Contact the
project maintainers privately (repository owner: Min Cho, via the
GitHub profile contact route) with:

- a description of the issue and its impact,
- steps to reproduce against synthetic data only,
- affected component (backend, web, ADK service, Go/Rust services,
  mobile).

You will get an acknowledgement, and a fix will be prepared before
any public disclosure. Please give the maintainers a reasonable
window to remediate before disclosing elsewhere.

## Absolute rules

- **Never** include real patient data or anything resembling PHI in
  an issue, report, log excerpt, or test fixture. Use synthetic data.
- Never commit credentials, tokens, or keys. Tokens stored by the
  system are hashes; private keys for signing or encryption live
  outside the repository by design.
- If you discover what you believe is real PHI or a live credential
  in this repository, report it privately as above and do not copy,
  download, or redistribute it.

## Scope notes

Authentication in production deployments is delegated to an external
OIDC provider (Keycloak); the in-app account system manages identity
records and governance, not passwords. Break-glass access, audit
chaining, and encryption behaviour are part of the clinical safety
model — changes to them require maintainer review.
