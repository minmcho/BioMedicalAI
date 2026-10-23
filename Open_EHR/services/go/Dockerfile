# Platform pass: Go tier image (MLLP gateway + edge node).
# Multi-stage: build with the Go toolchain image, ship a minimal
# runtime image holding both binaries. The compose file selects the
# binary per service (`command:`). Static artefact — Docker is not
# available on the build VM, so this image is NOT RUN-verified; the
# binaries themselves are verified by `go test ./...` and local runs.
FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -o /out/mllp-gateway ./cmd/mllp-gateway \
    && CGO_ENABLED=0 go build -trimpath -o /out/edge-node ./cmd/edge-node

FROM alpine:3.20
RUN adduser -D -H opencare
COPY --from=build /out/mllp-gateway /usr/local/bin/mllp-gateway
COPY --from=build /out/edge-node /usr/local/bin/edge-node
USER opencare
CMD ["mllp-gateway"]
