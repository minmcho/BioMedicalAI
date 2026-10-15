# Contracts

Single source of truth for API shapes. Clients (web TypeScript, Swift,
Kotlin, Python, Go, Rust) are generated from these — never hand-written.

- `fhir-resources.md` — FHIR R4 resources and profiles used, phase by phase.
- `schema.graphql` — GraphQL BFF schema **stub** (frontend aggregation lane
  only; resolvers land in a later phase — there is no GraphQL server yet).
- `internal.proto` — internal gRPC contract **stub** for service-to-service
  calls (FastAPI core ↔ Go API tier ↔ Rust compute). No gRPC server yet.

Contract changes are reviewed like schema migrations; a breaking change must
be called out in the PR and in the phase SQA report.
