# Service contracts — polyglot tier boundaries

Phase 17 (BD-67, ARC-07) versioned the contracts for the boundaries
the polyglot build actually created:

| Contract | Package | Boundary | Status |
|---|---|---|---|
| `opencare/v1/terminology.proto` | `opencare.terminology.v1` | Core pipeline step `terminology-normalise` ↔ Rust `opencare-term` semantics | **Implemented over gRPC in Go** (`services/go/internal/termgrpc`, generated stubs in `services/go/gen/`, contract tests in `go test`) + Python stubs in `gen/python/` |
| `opencare/v1/deidentification.proto` | `opencare.deidentification.v1` | Core pipeline de-id steps ↔ Rust `opencare-deid` crate | Contract of record; the Rust side is a batch CLI, not a server — no gRPC implementation claimed |

Generated stubs (do not edit by hand):

- Go: `services/go/gen/opencare/v1/` — protoc 28.3 +
  protoc-gen-go (protobuf v1.36.6) + protoc-gen-go-grpc (grpc v1.73.1)
- Python: `gen/python/opencare/v1/` — grpcio-tools 1.73.1

Regenerate (from `services/`):

    protoc -I contracts --go_out=go/gen --go_opt=paths=source_relative \
      --go-grpc_out=go/gen --go-grpc_opt=paths=source_relative \
      opencare/v1/terminology.proto opencare/v1/deidentification.proto

The Phase 0 stub at `contracts/internal.proto` (repo root) is
superseded by these versioned contracts; it is retained because
other documents reference it.
