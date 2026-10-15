# SQA — standing per-feature gate

**No feature is "done" until every applicable check below passes and the
result is recorded in that phase's report under `docs/sqa/`.** A check that
cannot run is recorded as NOT RUN with the reason — never silently skipped,
never reported as passed.

For every feature, in order:

1. **Unit + API tests** — new behaviour has pytest coverage; the full suite
   passes (`python -m pytest -q` from `backend/`). Record the exact result line.
2. **FHIR validation** — resources validate resourceType and required fields;
   invalid payloads return 422 (negative tests exist). Full US Core profile
   validation is a named future step until the validator is wired in CI.
3. **RBAC negative tests** — for every new write/studio route, at least one
   test proves a wrong role gets 403 and an unauthenticated call gets 401.
4. **Audit assertions** — every new write path writes an AuditLog row, and a
   test asserts the hash chain still verifies (`app.audit.verify_chain`).
5. **Completeness / no-noop check** — trace each route/control to a real
   database effect and a test that observes it. A route that returns success
   without a persisted effect fails the gate and is cut, not shipped.
6. **Security checklist** — no secrets in code or fixtures; synthetic data
   only (no real PHI); inputs validated at the boundary; errors do not leak
   internals; new dependencies named with licences.
7. **Data integrity** — writes are transactional (resource + projection +
   audit commit together); lifecycle/state machines reject out-of-order
   transitions with 409 and a test.
8. **Docs** — README / contracts / PHASES updated in the same change; known
   limitations stated in the phase SQA report, honestly.

Release rule inherited from the System Studio work: end-to-end or cut.
Partial features are removed from a phase, not shipped disabled-and-broken
(pack-declared endpoints shipping `enabled: false` is the designed lifecycle,
not a partial feature).
