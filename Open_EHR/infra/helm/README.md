# OpenCare on Kubernetes — deployer path (Helm)

Chart: [`opencare/`](opencare/) (`Chart.yaml`, `values.yaml`,
`values-production.example.yaml`, `templates/`). It is the recorded
scale-out option for OpenCare: the same topology as
[`docker-compose.yml`](../../docker-compose.yml), expressed as Kubernetes
manifests, plus the ADK runtime which compose does not yet carry.

> **Evidence boundary.** This chart was validated in the build
> environment by `helm lint` and `helm template` rendering only
> (see `~/workspace/phase17_work/helm/RESULTS.md` in the build record).
> **Rendering/config validation is not runtime proof.** No Docker
> runtime and no Kubernetes cluster existed in that environment, so
> **no local-cluster install was run there** — the procedure below is
> the path a deployer *with* Docker/a cluster follows. It is labelled
> as a procedure, not as evidence it ran in the build environment.
> Production-cluster operation remains a deployer activity.

## 1. What the chart deploys

| Chart component | Compose service | Image (build context) | Port(s) | Notes |
|---|---|---|---|---|
| `backend` | `app` | `backend/Dockerfile` | 8000 | FastAPI core; `GET /health` probes |
| `mllp` | `mllp` | `backend/Dockerfile` | 2575/TCP | Same image, `python -m app.mllp_server` |
| `web` | `web` | `web/Dockerfile` | 8080 | nginx-unprivileged static client |
| `goGateway` (`mllp-gateway`) | `mllp-gateway` | `services/go/Dockerfile` (`mllp-gateway`) | 2575 + 8081 health | Durable spool on a PVC; keep 1 replica per endpoint |
| `edgeNode` | `edge-node` (profile `edge`) | `services/go/Dockerfile` (`edge-node`) | 8082 | **Disabled by default** — per-site install, as in compose |
| `adk` | — (not in compose) | build from `backend/adk/` (see §3) | 8100 | Agent runtime; honest-off (503) with no model configured |
| `postgresql` (bundled) | `db` | `pgvector/pgvector:pg16` | 5432 | **Local/eval only** — disable for production (§4) |
| `valkey` (bundled) | `valkey` | `valkey/valkey:8` | 6379 | Disable + `valkey.externalUrl` for a managed cache |
| `rustJobs` | — (no compose service) | build from `services/rust/` | — | `opencare-deid` / `opencare-term` are CLI pipeline steps, not services; optional one-shot Jobs, disabled by default |

Faithfulness notes / known differences from compose:

- Compose builds images inline (`build:`); Kubernetes cannot — build
  and push the images first (§3) and name them in values.
- Compose's `depends_on: service_healthy` has no direct equivalent;
  the chart uses readiness probes + normal Kubernetes retry behaviour.
  The backend may log connection retries until PostgreSQL is ready.
- The web image's `nginx.conf` proxies same-origin API paths to
  `http://app:8000` (the compose service name). Under this chart the
  backend Service is `<release>-backend`. **Rebuild the web image with
  the nginx upstream matching your release name** (or install with
  release name `app`-compatible naming / patch the conf) — until then
  the SPA loads but its same-origin API calls will not reach the
  backend. `VITE_API_BASE`/`VITE_AUTH_MODE` remain build-time args.
- The warehouse is a *second* database (`WAREHOUSE_DATABASE_URL`),
  not a separate compose service; the chart wires it the same way.
- `smart-app/` is a SMART-on-FHIR companion launched from an EHR, not
  a server component; it is intentionally not a chart workload.

## 2. Prerequisites

- Kubernetes cluster (any conformant distribution; a local
  kind/minikube cluster is fine for evaluation), `kubectl` configured.
- Helm v3 (`helm version` — the chart was authored/validated with
  v3.16.3).
- A container registry reachable from the cluster (or a local cluster
  image-load mechanism, §6).
- An Ingress controller if `ingress.enabled=true`; cert-manager (or
  pre-created TLS Secrets) for TLS.
- For production: a deployer-managed PostgreSQL (with the `pgvector`
  extension available) and, optionally, a managed Valkey/Redis and a
  second warehouse database.

## 3. Build and publish the images

```sh
# from the repository root — tag consistently with your values file
docker build -t <registry>/opencare/backend:0.1.0    backend/
docker build -t <registry>/opencare/web:0.1.0        web/
docker build -t <registry>/opencare/go-services:0.1.0 services/go/
# ADK: no Dockerfile ships in backend/adk/ — build one equivalent to
# agents/Dockerfile (python:3.12-slim, COPY requirements.txt + app,
# EXPOSE 8100, uvicorn app.main:app --port 8100):
docker build -t <registry>/opencare/adk:0.1.0        backend/adk/   # with such a Dockerfile added by the deployer
# Rust tools (only if rustJobs are wanted): a deployer-authored image
# carrying the release binaries from `cargo build --release` in services/rust/.
for i in backend web go-services adk; do docker push <registry>/opencare/$i:0.1.0; done
```

No application code changes are needed or made by this chart.

## 4. External database (the production shape)

Production databases are deployer-managed — the chart never owns them:

```yaml
# in your values file (see opencare/values-production.example.yaml)
postgresql: { enabled: false }
externalDatabase: { host: postgres.example.org, port: 5432, name: openehr, user: openehr }
backend:
  databaseUrlSecret: { name: opencare-secrets, key: database-url }
  warehouse: { databaseUrlSecret: { name: opencare-secrets, key: warehouse-database-url } }
valkey: { enabled: false, externalUrl: "redis://valkey.example.org:6379/0" }
```

With `postgresql.enabled=false` no StatefulSet/PVC is rendered and the
backend takes its full `DATABASE_URL` from the referenced Secret.
Keeping `postgresql.enabled=true` renders the bundled
`pgvector/pgvector:pg16` StatefulSet for local/evaluation clusters
only; protect it the same way (its password also comes only from a
Secret reference).

## 5. Secrets — by reference only, never in values

The chart **creates no Secrets and contains no secret values**. Values
files name Secrets + keys; you create the Secrets out-of-band
(Secret manager, SealedSecrets, ExternalSecrets, or `kubectl`):

```sh
kubectl create namespace opencare
kubectl -n opencare create secret generic opencare-secrets \
  --from-literal=database-url='<full DATABASE_URL from your vault>' \
  --from-literal=warehouse-database-url='<full WAREHOUSE_DATABASE_URL>' \
  --from-literal=db-password='<db password>' \
  --from-literal=gateway-token='<devices.manage-capable gateway token>'
# only if the ADK uses a cloud model provider:
kubectl -n opencare create secret generic opencare-llm-secrets --from-literal='<provider key name>=<value>'
```

Referenced keys: `database-url`, `warehouse-database-url`,
`db-password`, `gateway-token` (see `values.yaml` for the exact
`databaseUrlSecret` / `passwordSecret` / `gatewayTokenSecret` fields).
TLS: reference a pre-created Secret in `ingress.tls[].secretName`.

## 6. Install — including the local-cluster procedure

### Production / staging cluster

```sh
helm lint infra/helm/opencare -f my-values.yaml
helm template opencare infra/helm/opencare -n opencare -f my-values.yaml | head   # eyeball the render
helm upgrade --install opencare infra/helm/opencare -n opencare --create-namespace -f my-values.yaml
kubectl -n opencare rollout status deploy/opencare-backend
kubectl -n opencare get pods,svc,ingress
curl -fsS https://<your-ingress-host>/            # web
kubectl -n opencare port-forward svc/opencare-backend 8000:8000   # then: curl http://127.0.0.1:8000/health
```

### Local-cluster (kind-class) procedure — for a deployer WITH Docker

*Procedure only — NOT RUN in the chart's build environment (no Docker
runtime there). Follow it on a machine with Docker:*

```sh
kind create cluster --name opencare          # or: minikube start
# build without a registry, then load the images into the cluster:
docker build -t opencare/backend:0.1.0 backend/ && docker build -t opencare/web:0.1.0 web/ \
  && docker build -t opencare/go-services:0.1.0 services/go/
kind load docker-image opencare/backend:0.1.0 opencare/web:0.1.0 opencare/go-services:0.1.0 --name opencare
kubectl create namespace opencare
kubectl -n opencare create secret generic opencare-secrets \
  --from-literal=database-url='postgresql+psycopg://opencare:<local-dev-password>@opencare-postgresql:5432/openehr' \
  --from-literal=db-password='<local-dev-password>' --from-literal=gateway-token='<local-dev-token>'
helm upgrade --install opencare infra/helm/opencare -n opencare   # default values: bundled PG + Valkey
kubectl -n opencare wait --for=condition=ready pod --all --timeout=300s
kubectl -n opencare port-forward svc/opencare-web 8080:8080      # http://127.0.0.1:8080/
```

(The ADK image needs the deployer Dockerfile from §3; for a backend-only
smoke test add `--set adk.enabled=false`.)

## 7. Ingress / TLS

`ingress.enabled=true` fronts **only the web Service**; the API is
reached same-origin through the web nginx proxy (subject to the §1
nginx-upstream note). Set `ingress.className`, `ingress.hosts`, and
`ingress.tls: [{secretName: opencare-tls, hosts: [your-host]}]`.
With cert-manager, annotate the Ingress
(`cert-manager.io/cluster-issuer`) and let it provision the named
Secret — the chart still stores no certificate material. The MLLP
services (2575/TCP) are raw TCP, not HTTP: expose them via a
`LoadBalancer`/`NodePort` Service type or your TCP load balancer,
never through the HTTP Ingress.

## 8. Configuration reference, upgrade, removal

- Images/replicas/resources per workload, HPA (`backend.autoscaling`),
  probes, node placement: see the commented sections of
  `opencare/values.yaml`.
- The governed application configuration (Studio packs, agent configs,
  terminology) lives in the database/Studio as usual — Helm values
  carry *deployment* configuration only (hosts, replicas, resources,
  Secret references, non-secret env via `config:` / `config.extraEnv`).
- Upgrade: bump image tags in your values file, re-run the
  `helm upgrade --install` command above. The database schema is
  created by the application (`create_all`, no migrations yet) —
  back up the external database before upgrading, as with compose.
- Remove: `helm uninstall opencare -n opencare`. PVCs and any
  external database are retained by design; delete them explicitly
  if the environment is disposable.
