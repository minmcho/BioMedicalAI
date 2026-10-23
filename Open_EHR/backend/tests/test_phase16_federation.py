"""Phase 16 (WS-A2) — BD-36 federation + BD-37 reconciliation.

In-process tests use the standard conftest `client` fixture (the
coordinator mounts app.federation's router centrally at integration).

The cross-instance proof is the heart of the suite: it spawns TWO
real uvicorn subprocesses on localhost with separate DATABASE_URLs
(the Phase 8 two-process precedent), seeds peer B with a patient +
clinical records + a Consent permit for node A, and then, from A:
a full query returns data, references mode returns NO clinical
payload ("data behind glass"), a patient with no Consent permit is
refused 403, a pull stages records without touching the chart,
reconciliation accept-new writes the chart WITH the provenance
extension, link-existing leaves the chart unchanged, reject closes
the staged record, and audit rows are asserted in BOTH instances'
databases. Synthetic data only; no live TEFCA network is involved.
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app import federation as fed
from app import fhir
from app.models import AuditLog, FhirResource
from tests.conftest import ADMIN, CLINICIAN, VIEWER

BACKEND_DIR = Path(__file__).resolve().parents[1]

COND_CODE = "fed-secret-cond-7341"
ADMIN_HDR = {"Authorization": "Bearer admin-token"}


# ---------------------------------------------------------------------------
# In-process: peers view + inbound consent gate
# ---------------------------------------------------------------------------

def test_peers_view_shows_seeded_published_config(client):
    r = client.get("/federation/peers", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["node"]["name"] == "local-opencare"
    assert body["peers"] == []  # seeded with an EMPTY peer list
    assert body["config"] == {"kind": "integration", "key": "federation",
                              "version": 1}
    # Viewer holds federation.read by the proposed defaults; a patient
    # token is not a staff identity at all for this surface.
    assert client.get("/federation/peers", headers=VIEWER).status_code == 200


def test_inbound_refuses_without_consent_and_audits(client):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": "inp-noconsent",
        "name": [{"family": "SyntheticFed", "given": ["Nia"]}],
        "gender": "female", "birthDate": "1990-05-01"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.get("/federation/inbound/Patient",
                   params={"patient_id": "inp-noconsent",
                           "purpose": "treatment"},
                   headers={**ADMIN, "X-OpenCare-Peer": "node-x"})
    assert r.status_code == 403, r.text
    assert "no Consent" in r.json()["detail"]
    with client.app.state.session_factory() as session:
        rows = session.scalars(select(AuditLog).where(
            AuditLog.action == "federation.query.in")).all()
    assert rows, "the refusal must be audited on the responding node"
    payload = rows[-1].payload
    assert payload["outcome"] == "refused"
    assert payload["peer"] == "node-x"
    assert rows[-1].entity == "Patient/inp-noconsent"


def test_inbound_patient_not_found_is_404(client):
    r = client.get("/federation/inbound/Patient",
                   params={"patient_id": "ghost"},
                   headers={**ADMIN, "X-OpenCare-Peer": "node-x"})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# In-process: reconciliation staging -> diff -> actions
# ---------------------------------------------------------------------------

def _stage(client, *, peer="peer-b", rtype="Observation",
           ext_id="ext-obs-1", patient_ref="Patient/rec-pat-1",
           payload=None):
    with client.app.state.session_factory() as session:
        row = fed.ExternalRecord(
            source_peer=peer, resource_type=rtype, external_id=ext_id,
            patient_ref=patient_ref,
            payload=payload or {
                "resourceType": rtype, "id": ext_id, "status": "final",
                "code": {"coding": [{"system": "http://loinc.org",
                                     "code": "2345-7",
                                     "display": "Glucose"}]},
                "subject": {"reference": patient_ref},
                "valueQuantity": {"value": 123.0, "unit": "mg/dL"}},
            provenance={"peer": peer, "external_id": ext_id,
                        "external_ref": f"{rtype}/{ext_id}"},
            status="staged")
        session.add(row)
        session.commit()
        return row.id


def test_reconciliation_diff_and_accept_new_with_provenance(client):
    record_id = _stage(client)
    view = client.get("/federation/reconciliation/rec-pat-1",
                      headers=ADMIN)
    assert view.status_code == 200, view.text
    item = view.json()["items"][0]
    assert item["status"] == "staged"
    assert item["chart_exists"] is False
    fields = {f["field"]: f for f in item["fields"]}
    assert fields["valueQuantity"]["external"] == {"value": 123.0,
                                                   "unit": "mg/dL"}
    assert fields["valueQuantity"]["chart"] is None
    assert fields["valueQuantity"]["differs"] is True
    # Nothing staged is in the chart yet.
    assert client.get("/fhir/Observation/ext-obs-1",
                      headers=ADMIN).status_code == 404

    r = client.post(
        f"/federation/reconciliation/items/{record_id}/accept-new",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["record"]["status"] == "accepted-new"
    chart = client.get("/fhir/Observation/ext-obs-1", headers=ADMIN)
    assert chart.status_code == 200, chart.text
    extensions = chart.json().get("extension") or []
    prov = [e for e in extensions
            if e.get("url") == "urn:opencare:extension/provenance"]
    assert prov, chart.text
    assert "peer-b" in prov[0]["valueString"]
    assert "ext-obs-1" in prov[0]["valueString"]
    # A decided record cannot be decided again.
    r = client.post(
        f"/federation/reconciliation/items/{record_id}/accept-new",
        headers=ADMIN)
    assert r.status_code == 409, r.text
    with client.app.state.session_factory() as session:
        actions = [a.action for a in session.scalars(
            select(AuditLog).where(
                AuditLog.action.like("federation.reconcile.%"))).all()]
    assert "federation.reconcile.accept_new" in actions


def test_reconciliation_link_existing_leaves_chart_unchanged(client):
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "id": "local-obs-1",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org",
                             "code": "2345-7", "display": "Glucose"}]},
        "subject": {"reference": "Patient/rec-pat-1"},
        "valueQuantity": {"value": 100.0, "unit": "mg/dL"}},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    before = client.get("/fhir/Observation/local-obs-1",
                        headers=ADMIN).json()
    record_id = _stage(client, ext_id="ext-obs-2")
    r = client.post(
        f"/federation/reconciliation/items/{record_id}/link-existing",
        json={"chart_ref": "Observation/local-obs-1"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["record"]["status"] == "linked"
    assert r.json()["record"]["linked_ref"] == "Observation/local-obs-1"
    after = client.get("/fhir/Observation/local-obs-1",
                       headers=ADMIN).json()
    assert after == before  # chart byte-identical: link records only


def test_reconciliation_reject_and_unsupported_type_degrade(client):
    record_id = _stage(client, ext_id="ext-obs-3")
    r = client.post(
        f"/federation/reconciliation/items/{record_id}/reject",
        json={"reason": "duplicate of local record"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["record"]["status"] == "rejected"
    assert client.get("/fhir/Observation/ext-obs-3",
                      headers=ADMIN).status_code == 404
    # A staged record of a type the FHIR surface does not support at
    # runtime degrades with a clear 409 and STAYS staged.
    bogus_id = _stage(client, rtype="FedBogusType", ext_id="bogus-1",
                      payload={"resourceType": "FedBogusType",
                               "id": "bogus-1"})
    r = client.post(
        f"/federation/reconciliation/items/{bogus_id}/accept-new",
        headers=ADMIN)
    assert r.status_code == 409, r.text
    assert "not supported" in r.json()["detail"]
    view = client.get("/federation/reconciliation/rec-pat-1",
                      headers=ADMIN).json()
    bogus = [i for i in view["items"] if i["external_id"] == "bogus-1"]
    assert bogus and bogus[0]["status"] == "staged"


def test_outbound_query_unknown_peer_is_404(client):
    r = client.post("/federation/query",
                    json={"peer": "no-such-peer", "patient_id": "x"},
                    headers=ADMIN)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Cross-instance proof: two real uvicorn subprocesses
# ---------------------------------------------------------------------------

_RUNNER = r"""
import os
from sqlalchemy import select

from app.federation import ensure_federation_defaults, router
from app.main import create_app
from app.models import FhirResource, StudioConfig

app = create_app()
app.include_router(router)

with app.state.session_factory() as session:
    ensure_federation_defaults(session)
    cfg = session.scalars(select(StudioConfig).where(
        StudioConfig.kind == "integration",
        StudioConfig.key == "federation",
        StudioConfig.status == "published")).first()
    peers = []
    if os.environ.get("FED_PEER_NAME"):
        peers = [{"name": os.environ["FED_PEER_NAME"],
                  "baseUrl": os.environ["FED_PEER_URL"], "enabled": True}]
    cfg.payload = {
        "node": {"name": os.environ["FED_NODE_NAME"],
                 "baseUrl": os.environ["FED_SELF_URL"],
                 "organization": "OpenCare federation test node"},
        "peers": peers}
    session.commit()
    if os.environ.get("FED_SEED_CONSENT") == "1":
        body = {
            "resourceType": "Consent", "id": "consent-fed-1",
            "status": "active",
            "scope": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/"
                          "consentscope", "code": "patient-privacy"}]},
            "category": [{"coding": [{
                "system": "http://loinc.org", "code": "59284-0"}]}],
            "patient": {"reference": "Patient/fed-pat-1"},
            "provision": {
                "type": "permit",
                "purpose": [{"coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/"
                              "v3-ActReason", "code": "treatment"}]}],
                "actor": [{"reference": {
                    "reference": "Organization/node-a"}}]}}
        row = FhirResource(resource_type="Consent",
                           resource_id="consent-fed-1", version=1,
                           body=body, patient_ref="Patient/fed-pat-1")
        session.add(row)
        session.commit()

import uvicorn
uvicorn.run(app, host="127.0.0.1", port=int(os.environ["FED_PORT"]))
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Node:
    def __init__(self, tmp_path, label, node_name, peer_name=None,
                 peer_url=None, seed_consent=False):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.db_path = tmp_path / f"{label}.db"
        runner = tmp_path / f"runner_{label}.py"
        runner.write_text(_RUNNER)
        env = dict(os.environ)
        env.update({
            "DATABASE_URL": f"sqlite:///{self.db_path}",
            "AUTH_MODE": "dev",
            "FED_PORT": str(self.port),
            "FED_NODE_NAME": node_name,
            "FED_SELF_URL": self.url,
            "PYTHONPATH": str(BACKEND_DIR),
        })
        if peer_name:
            env["FED_PEER_NAME"] = peer_name
            env["FED_PEER_URL"] = peer_url
        if seed_consent:
            env["FED_SEED_CONSENT"] = "1"
        log = open(tmp_path / f"{label}.log", "wb")
        self.proc = subprocess.Popen(
            [sys.executable, str(runner)], cwd=str(BACKEND_DIR), env=env,
            stdout=log, stderr=subprocess.STDOUT)
        self._wait_ready(tmp_path, label)

    def _wait_ready(self, tmp_path, label):
        deadline = time.time() + 90
        with httpx.Client(trust_env=False, timeout=5.0) as client:
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise AssertionError(
                        f"node {label} exited early:\n"
                        + (tmp_path / f"{label}.log").read_text())
                try:
                    if client.get(self.url + "/health").status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
        raise AssertionError(f"node {label} did not become ready")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def audit_actions(self) -> list[tuple[str, dict]]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro",
                               uri=True)
        try:
            rows = conn.execute(
                "SELECT action, payload FROM audit_log "
                "WHERE action LIKE 'federation.%' ORDER BY id").fetchall()
        finally:
            conn.close()
        return [(a, json.loads(p) if isinstance(p, str) else p)
                for a, p in rows]


def _seed_peer_b(node: _Node):
    with httpx.Client(trust_env=False, timeout=10.0) as http:
        for pid, family in (("fed-pat-1", "Federated"), ("fed-pat-2", "NoConsent")):
            r = http.post(node.url + "/fhir/Patient", json={
                "resourceType": "Patient", "id": pid,
                "name": [{"family": family, "given": ["Synth"]}],
                "gender": "female", "birthDate": "1980-01-15",
                "identifier": [{"system": "urn:opencare:mrn",
                                "value": f"MRN-{pid}"}]},
                headers=ADMIN_HDR)
            assert r.status_code == 201, r.text
        r = http.post(node.url + "/fhir/Condition", json={
            "resourceType": "Condition", "id": "cond-fed-1",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"coding": [{"system": "http://snomed.info/sct",
                                 "code": COND_CODE,
                                 "display": "Synthetic federated condition"}]},
            "subject": {"reference": "Patient/fed-pat-1"}}, headers=ADMIN_HDR)
        assert r.status_code == 201, r.text
        r = http.post(node.url + "/fhir/Observation", json={
            "resourceType": "Observation", "id": "obs-fed-1",
            "status": "final",
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": "2345-7", "display": "Glucose"}]},
            "subject": {"reference": "Patient/fed-pat-1"},
            "valueQuantity": {"value": 42.5, "unit": "mg/dL"}},
            headers=ADMIN_HDR)
        assert r.status_code == 201, r.text


def test_cross_instance_federation_end_to_end(tmp_path):
    node_b = _Node(tmp_path, "b", "peer-b", seed_consent=True)
    node_a = _Node(tmp_path, "a", "node-a", peer_name="peer-b",
                   peer_url=node_b.url)
    try:
        _seed_peer_b(node_b)
        with httpx.Client(trust_env=False, timeout=20.0) as http:
            # Peers view on A names B.
            peers = http.get(node_a.url + "/federation/peers",
                             headers=ADMIN_HDR).json()
            assert [p["name"] for p in peers["peers"]] == ["peer-b"]
            assert peers["node"]["name"] == "node-a"

            # FULL query returns the clinical data.
            r = http.post(node_a.url + "/federation/query", json={
                "peer": "peer-b", "patient_id": "fed-pat-1",
                "mode": "full", "purpose": "treatment"}, headers=ADMIN_HDR)
            assert r.status_code == 200, r.text
            full = r.json()
            assert full["patient"]["id"] == "fed-pat-1"
            dumped = json.dumps(full)
            assert COND_CODE in dumped
            assert "42.5" in dumped

            # REFERENCES mode: counts + refs + limited demographics,
            # and NO clinical payload anywhere.
            r = http.post(node_a.url + "/federation/query", json={
                "peer": "peer-b", "patient_id": "fed-pat-1",
                "mode": "references", "purpose": "treatment"},
                headers=ADMIN_HDR)
            assert r.status_code == 200, r.text
            refs = r.json()
            assert refs["counts"]["Observation"] >= 1
            assert {"reference": "Observation/obs-fed-1",
                    "resourceType": "Observation"} in refs["references"]
            dumped_refs = json.dumps(refs)
            assert COND_CODE not in dumped_refs
            assert "valueQuantity" not in dumped_refs
            assert "42.5" not in dumped_refs
            assert "MRN-fed-pat-1" not in dumped_refs

            # NO Consent permit -> the peer refuses 403.
            r = http.post(node_a.url + "/federation/query", json={
                "peer": "peer-b", "patient_id": "fed-pat-2",
                "mode": "full", "purpose": "treatment"}, headers=ADMIN_HDR)
            assert r.status_code == 403, r.text

            # PULL stages records; the chart stays untouched.
            r = http.post(node_a.url + "/federation/pull", json={
                "peer": "peer-b", "patient_id": "fed-pat-1",
                "purpose": "treatment"}, headers=ADMIN_HDR)
            assert r.status_code == 200, r.text
            pull = r.json()
            assert pull["staged_count"] >= 4  # Patient+Condition+Obs+Consent
            staged = {s["resource_type"]: s for s in pull["staged"]}
            assert http.get(node_a.url + "/fhir/Condition/cond-fed-1",
                            headers=ADMIN_HDR).status_code == 404
            assert http.get(node_a.url + "/fhir/Patient/fed-pat-1",
                            headers=ADMIN_HDR).status_code == 404

            # Reconciliation: side-by-side diff.
            view = http.get(
                node_a.url + "/federation/reconciliation/fed-pat-1",
                headers=ADMIN_HDR)
            assert view.status_code == 200, view.text
            items = {i["resource_type"]: i for i in view.json()["items"]}
            obs_fields = {f["field"]: f
                          for f in items["Observation"]["fields"]}
            assert obs_fields["valueQuantity"]["external"] == {
                "value": 42.5, "unit": "mg/dL"}
            assert obs_fields["valueQuantity"]["chart"] is None
            assert items["Observation"]["chart_exists"] is False

            # ACCEPT-NEW writes the chart WITH provenance.
            r = http.post(
                node_a.url + "/federation/reconciliation/items/"
                f"{staged['Observation']['id']}/accept-new",
                headers=ADMIN_HDR)
            assert r.status_code == 200, r.text
            chart = http.get(node_a.url + "/fhir/Observation/obs-fed-1",
                             headers=ADMIN_HDR)
            assert chart.status_code == 200, chart.text
            prov = [e for e in chart.json().get("extension") or []
                    if e.get("url")
                    == "urn:opencare:extension/provenance"]
            assert prov and "peer-b" in prov[0]["valueString"]
            assert "obs-fed-1" in prov[0]["valueString"]

            # LINK-EXISTING: link the staged Condition to a local one;
            # the local chart resource stays byte-identical.
            r = http.post(node_a.url + "/fhir/Condition", json={
                "resourceType": "Condition", "id": "cond-local-1",
                "clinicalStatus": {"coding": [{"code": "active"}]},
                "code": {"coding": [{"system": "http://snomed.info/sct",
                                     "code": COND_CODE,
                                     "display": "Synthetic local copy"}]},
                "subject": {"reference": "Patient/fed-pat-1"}},
                headers=ADMIN_HDR)
            assert r.status_code == 201, r.text
            local_before = http.get(
                node_a.url + "/fhir/Condition/cond-local-1",
                headers=ADMIN_HDR).json()
            r = http.post(
                node_a.url + "/federation/reconciliation/items/"
                f"{staged['Condition']['id']}/link-existing",
                json={"chart_ref": "Condition/cond-local-1"},
                headers=ADMIN_HDR)
            assert r.status_code == 200, r.text
            assert r.json()["record"]["status"] == "linked"
            assert http.get(node_a.url + "/fhir/Condition/cond-local-1",
                            headers=ADMIN_HDR).json() == local_before
            # The external Condition itself was NOT merged into chart.
            assert http.get(node_a.url + "/fhir/Condition/cond-fed-1",
                            headers=ADMIN_HDR).status_code == 404

            # Unsupported-type degrade (only while Consent is not on
            # this node's FHIR surface at runtime): clear 409.
            if "Consent" not in fhir.SUPPORTED:
                r = http.post(
                    node_a.url + "/federation/reconciliation/items/"
                    f"{staged['Consent']['id']}/accept-new",
                    headers=ADMIN_HDR)
                assert r.status_code == 409, r.text
                assert "not supported" in r.json()["detail"]

            # REJECT the staged Patient record; nothing is created.
            r = http.post(
                node_a.url + "/federation/reconciliation/items/"
                f"{staged['Patient']['id']}/reject",
                json={"reason": "identity not yet verified"},
                headers=ADMIN_HDR)
            assert r.status_code == 200, r.text
            assert r.json()["record"]["status"] == "rejected"
            assert http.get(node_a.url + "/fhir/Patient/fed-pat-1",
                            headers=ADMIN_HDR).status_code == 404

        # Audit rows on BOTH instances.
        a_actions = node_a.audit_actions()
        b_actions = node_b.audit_actions()
        a_names = [a for a, _ in a_actions]
        b_names = [a for a, _ in b_actions]
        assert "federation.query.out" in a_names
        assert "federation.pull" in a_names
        assert "federation.reconcile.accept_new" in a_names
        assert "federation.reconcile.link_existing" in a_names
        assert "federation.reconcile.reject" in a_names
        assert "federation.query.in" in b_names
        b_outcomes = [p.get("outcome") for a, p in b_actions
                      if a == "federation.query.in"]
        assert "permit" in b_outcomes
        assert "refused" in b_outcomes
    finally:
        node_a.stop()
        node_b.stop()
