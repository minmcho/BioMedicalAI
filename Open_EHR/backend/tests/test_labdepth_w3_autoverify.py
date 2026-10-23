"""Lab Depth W3 (LAB-D7) — autoverification maturity: governed
per-bench rule sets (published Studio config, seeded unpublished),
auto-RELEASE under rules with full rule traces, the hold queue, the
rule taxonomy (AMR / interference / conditional delta / cross-
analyte trap / absurd value), CAP GEN.43890 delta parity between
the auto and manual paths, and autoverification-rate monitoring.

Synthetic data only (fabricated patients and values). Routers are
mounted here when app.main has not wired them yet (coordinator
wires via the W3 INTEGRATION NOTE); the mount is skipped when
already wired."""
import pytest
from fastapi.testclient import TestClient

from app import lab_analyser as lab_analyser_mod
from app import lab_autoverify as lab_autoverify_mod
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN, VIEWER


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/w3v.db")
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/lab/analyser/instruments" not in paths:
        app.include_router(lab_analyser_mod.router)
    if "/lab/autoverify/status" not in paths:
        app.include_router(lab_autoverify_mod.router)
    with TestClient(app) as c:
        with app.state.session_factory() as session:
            lab_analyser_mod.ensure_lab_analyser_defaults(session)
            lab_autoverify_mod.ensure_lab_autoverify_defaults(session)
        yield c

BMP = "51990-0"
CBC = "58410-2"


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic", "given": ["Lab"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _order(client, code, oid):
    r = client.post("/lab/orders",
                    json={"code": code, "id": oid,
                          "subject": {"reference": "Patient/pat-1"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _to_received(client, order_id):
    for status in ("collected", "received"):
        r = client.post(f"/lab/orders/{order_id}/transition",
                        json={"to_status": status}, headers=CLINICIAN)
        assert r.status_code == 200, r.text


def _bmp(creatinine=1.0, glucose=95, potassium=4.4, sodium=140):
    return [{"code": "2345-7", "value": glucose},
            {"code": "3094-0", "value": 12},
            {"code": "2160-0", "value": creatinine},
            {"code": "2951-2", "value": sodium},
            {"code": "2823-3", "value": potassium}]


def _enter(client, order_id, results, expect=201):
    r = client.post(f"/lab/orders/{order_id}/results",
                    json={"results": results}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _publish_av(client, concepts):
    payload = {"name": "W3 test autoverification rule sets",
               "system": "urn:opencare:lab-autoverify",
               "concepts": concepts}
    r = client.post("/studio/config/terminology/lab-autoverify",
                    json=payload, headers=ADMIN)
    if r.status_code == 409:
        # the seeded template draft is open: replace ITS values and
        # publish that version instead (one open draft per key)
        history = client.get("/studio/config/terminology/lab-autoverify",
                             headers=ADMIN).json()
        draft = next(v for v in history if v["status"] == "draft")
        version = draft["version"]
        r = client.put(
            f"/studio/config/terminology/lab-autoverify/versions/{version}",
            json=payload, headers=ADMIN)
        assert r.status_code == 200, r.text
    else:
        assert r.status_code == 201, r.text
        version = r.json()["version"]
    r = client.post(
        f"/studio/config/terminology/lab-autoverify/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _chem_set(**overrides):
    concept = {"code": "chemistry", "display": "Chemistry bench rule set",
               "active": True, "enabled": True, "holdOnCritical": True,
               "rules": [
                   {"id": "chem-glucose-amr", "type": "amr",
                    "analyteCode": "2345-7", "low": 20.0, "high": 600.0},
                   {"id": "chem-potassium-amr", "type": "amr",
                    "analyteCode": "2823-3", "low": 1.5, "high": 10.0},
                   {"id": "chem-interference", "type": "interference",
                    "flagCodes": ["HEM", "ICT", "LIP"]},
                   {"id": "chem-creatinine-delta", "type": "delta",
                    "analyteCode": "2160-0", "absolute": 0.3,
                    "windowHours": 72,
                    "condition": {"outsideReferenceOnly": True}},
                   {"id": "chem-absurd-potassium", "type": "absurd",
                    "analyteCode": "2823-3", "low": 0.5, "high": 14.0},
               ]}
    concept.update(overrides)
    return concept


def _resulted_bmp(client, oid, **kw):
    order = _order(client, BMP, oid)
    _to_received(client, order["id"])
    return order, _enter(client, order["id"], _bmp(**kw))


def _events(client, bench=None):
    params = {"bench": bench} if bench else None
    r = client.get("/lab/autoverify/events", params=params, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ---------------------------------------------------------------------------
# Governance: seeded OFF — no bench auto-releases until ITS set publishes
# ---------------------------------------------------------------------------

def test_unpublished_bench_never_auto_releases(client):
    _patient(client)
    _order_row, outcome = _resulted_bmp(client, "ord-av-1")
    assert outcome["auto_released"] is False
    assert outcome["auto_verified"] is False  # legacy catalog path is OFF too
    assert outcome["autoverify"] is None
    detail = client.get("/lab/orders/ord-av-1", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"
    status = client.get("/lab/autoverify/status", headers=CLINICIAN).json()
    chem = next(b for b in status["benches"] if b["bench"] == "chemistry")
    assert chem["state"] == "unpublished" and chem["enabled"] is False
    assert _events(client) == []


def test_published_but_disabled_set_never_releases(client):
    _publish_av(client, [_chem_set(enabled=False)])
    _patient(client)
    _order_row, outcome = _resulted_bmp(client, "ord-av-2")
    assert outcome["auto_released"] is False
    status = client.get("/lab/autoverify/status", headers=CLINICIAN).json()
    chem = next(b for b in status["benches"] if b["bench"] == "chemistry")
    assert chem["state"] == "published-disabled"


# ---------------------------------------------------------------------------
# Auto-RELEASE under a published set, with the full rule trace
# ---------------------------------------------------------------------------

def test_published_set_auto_releases_clean_result_with_trace(client):
    _publish_av(client, [_chem_set()])
    _patient(client)
    order, outcome = _resulted_bmp(client, "ord-av-3")
    assert outcome["auto_released"] is True
    assert outcome["auto_verified"] is True
    assert outcome["autoverify"]["decision"] == "release"
    # the release is real: order released, report final + issued, and
    # the results are visible in the patient portal (tag removed)
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "released"
    report = detail["reports"][0]
    assert report["status"] == "final" and report["issued"]
    # the recorded event carries the full trace: every standing gate
    # and every configured rule, each with its outcome
    events = _events(client, bench="chemistry")
    assert len(events) == 1 and events[0]["decision"] == "released"
    assert events[0]["hold_reasons"] == []
    by_id = {t["rule_id"]: t for t in events[0]["trace"]}
    for rule_id in ("qc-gate", "instrument-path", "qualitative-guard",
                    "critical-posture", "chem-glucose-amr",
                    "chem-potassium-amr", "chem-interference",
                    "chem-creatinine-delta", "chem-absurd-potassium"):
        assert rule_id in by_id, rule_id
        assert by_id[rule_id]["outcome"] in ("pass", "skip")
    assert client.get("/lab/verification-queue",
                      headers=CLINICIAN).json()["total"] == 0


# ---------------------------------------------------------------------------
# Each rule type holds, with its named reason
# ---------------------------------------------------------------------------

def test_amr_rule_holds_with_named_reason(client):
    # holdOnCritical off in this set so the AMR hold is isolated
    _publish_av(client, [_chem_set(holdOnCritical=False)])
    _patient(client)
    _order_row, outcome = _resulted_bmp(client, "ord-amr", glucose=700)
    assert outcome["auto_released"] is False
    assert outcome["autoverify"]["decision"] == "hold"
    reasons = outcome["autoverify"]["hold_reasons"]
    assert any("chem-glucose-amr" in r and "AMR" in r for r in reasons)
    holds = client.get("/lab/autoverify/holds", headers=CLINICIAN).json()
    assert holds["total"] == 1
    assert holds["items"][0]["order_id"] == "ord-amr"
    # held means untouched: the draft awaits the manual path
    detail = client.get("/lab/orders/ord-amr", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "resulted"


def test_interference_flag_holds_with_named_reason(client):
    _publish_av(client, [_chem_set()])
    _patient(client)
    order = _order(client, BMP, "ord-hil")
    _to_received(client, order["id"])
    results = _bmp()
    results[0] = {"code": "2345-7", "value": 95, "flags": ["HEM"]}
    outcome = _enter(client, order["id"], results)
    assert outcome["auto_released"] is False
    reasons = outcome["autoverify"]["hold_reasons"]
    assert any("chem-interference" in r and "HEM" in r for r in reasons)


def test_absurd_value_holds_with_named_reason(client):
    # AMR widened past the absurd bound so ONLY the absurd trap fires
    concept = _chem_set(holdOnCritical=False)
    concept["rules"] = [
        {"id": "chem-potassium-amr", "type": "amr",
         "analyteCode": "2823-3", "low": 1.5, "high": 14.5},
        {"id": "chem-absurd-potassium", "type": "absurd",
         "analyteCode": "2823-3", "low": 0.5, "high": 14.0},
    ]
    _publish_av(client, [concept])
    _patient(client)
    _order_row, outcome = _resulted_bmp(client, "ord-absurd", potassium=14.2)
    assert outcome["auto_released"] is False
    reasons = outcome["autoverify"]["hold_reasons"]
    assert any("chem-absurd-potassium" in r and "absurd" in r
               for r in reasons), reasons
    assert not any("chem-potassium-amr" in r for r in reasons), reasons


def test_cross_analyte_trap_holds_and_clean_pair_releases(client):
    haem = {"code": "haematology", "display": "Haematology rule set",
            "active": True, "enabled": True, "holdOnCritical": False,
            "rules": [{"id": "haem-rule-of-three", "type": "cross-analyte",
                       "mode": "ratio", "codeA": "718-7", "codeB": "4544-3",
                       "factor": 3.0, "tolerancePct": 10.0}]}
    _publish_av(client, [haem])
    _patient(client)
    # Hct 41 with Hgb 10 breaks the rule of three (expected ~30)
    order = _order(client, CBC, "ord-x1")
    _to_received(client, order["id"])
    outcome = _enter(client, order["id"], [
        {"code": "718-7", "value": 10.0}, {"code": "4544-3", "value": 41.0},
        {"code": "6690-2", "value": 7.0}, {"code": "777-3", "value": 260}])
    assert outcome["auto_released"] is False
    reasons = outcome["autoverify"]["hold_reasons"]
    assert any("haem-rule-of-three" in r and "cross-analyte" in r
               for r in reasons), reasons
    # a consistent pair releases
    order = _order(client, CBC, "ord-x2")
    _to_received(client, order["id"])
    outcome = _enter(client, order["id"], [
        {"code": "718-7", "value": 13.5}, {"code": "4544-3", "value": 41.0},
        {"code": "6690-2", "value": 7.0}, {"code": "777-3", "value": 260}])
    assert outcome["auto_released"] is True


def test_conditional_delta_holds_only_when_condition_met(client):
    _publish_av(client, [_chem_set()])
    _patient(client)
    # baseline: creatinine 1.0 auto-releases
    _o, outcome = _resulted_bmp(client, "ord-d1", creatinine=1.0)
    assert outcome["auto_released"] is True
    # inside the reference range the conditional delta rule SKIPS —
    # the set releases even though a prior baseline exists
    _o, outcome = _resulted_bmp(client, "ord-d2", creatinine=1.05)
    assert outcome["auto_released"] is True
    delta_trace = next(t for t in outcome["autoverify"]["trace"]
                       if t["rule_id"] == "chem-creatinine-delta")
    assert delta_trace["outcome"] == "skip"
    # outside the range the condition is met and the delta bites:
    # 1.05 -> 1.6 is a 0.55 change against the 0.3 governed threshold
    _o, outcome = _resulted_bmp(client, "ord-d3", creatinine=1.6)
    assert outcome["auto_released"] is False
    reasons = outcome["autoverify"]["hold_reasons"]
    assert any("chem-creatinine-delta" in r and "delta" in r
               for r in reasons), reasons


# ---------------------------------------------------------------------------
# CAP GEN.43890 parity: the same evaluation for auto and manual paths
# ---------------------------------------------------------------------------

def test_delta_identical_for_auto_and_manual_paths(client):
    _publish_av(client, [_chem_set()])
    _patient(client)
    _o, outcome = _resulted_bmp(client, "ord-p1", creatinine=1.0)
    assert outcome["auto_released"] is True  # released baseline 1.0
    # a fresh received order for the same patient
    order = _order(client, BMP, "ord-p2")
    _to_received(client, order["id"])
    results = _bmp(creatinine=1.6)
    # the dry-run (manual-path parity surface) judges first, writing
    # nothing
    r = client.post("/lab/autoverify/evaluate",
                    json={"order_id": "ord-p2", "results": results},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    dry = r.json()
    assert dry["decision"] == "hold"
    # the dry-run wrote nothing: the only recorded event is ord-p1's
    # release; nothing exists for ord-p2 yet
    assert [e["order_id"] for e in _events(client)] == ["ord-p1"]
    # the real entry is judged by the SAME function: identical reasons
    outcome = _enter(client, order["id"], results)
    assert outcome["auto_released"] is False
    assert outcome["autoverify"]["hold_reasons"] == dry["hold_reasons"]
    # and the human verification queue flags the same delta through
    # app.lab._delta_flag (the manual path's own computation)
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    item = next(i for i in queue["items"] if i["order_id"] == "ord-p2")
    assert [f["code"] for f in item["delta_flags"]] == ["2160-0"]
    assert item["delta_flags"][0]["previous_value"] == 1.0


# ---------------------------------------------------------------------------
# Hold queue disposition + the manual path stays intact
# ---------------------------------------------------------------------------

def test_hold_acknowledge_and_manual_path_intact(client):
    _publish_av(client, [_chem_set(holdOnCritical=False)])
    _patient(client)
    order, outcome = _resulted_bmp(client, "ord-hold", glucose=700)
    report_id = outcome["report"]["id"]
    holds = client.get("/lab/autoverify/holds", headers=CLINICIAN).json()
    event_id = holds["items"][0]["id"]
    # a viewer cannot disposition a hold (verify capability required)
    assert client.post(f"/lab/autoverify/holds/{event_id}/acknowledge",
                       json={}, headers=VIEWER).status_code == 403
    r = client.post(f"/lab/autoverify/holds/{event_id}/acknowledge",
                    json={"note": "Reviewed; repeat dilution requested"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "acknowledged"
    assert r.json()["acknowledged_by"] == "dev-clinician"
    assert client.post(f"/lab/autoverify/holds/{event_id}/acknowledge",
                       json={}, headers=CLINICIAN).status_code == 409
    # the manual verify/release path is untouched and works on the
    # held draft exactly as before D7
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200
    assert client.post(f"/lab/reports/{report_id}/release",
                       headers=CLINICIAN).status_code == 200
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "released"


# ---------------------------------------------------------------------------
# Autoverification-rate monitoring
# ---------------------------------------------------------------------------

def test_av_rate_math_per_bench(client):
    _publish_av(client, [_chem_set(holdOnCritical=False)])
    _patient(client)
    _resulted_bmp(client, "ord-r1")
    _resulted_bmp(client, "ord-r2")
    _resulted_bmp(client, "ord-r3", glucose=700)        # AMR hold
    order = _order(client, BMP, "ord-r4")
    _to_received(client, order["id"])
    flagged = _bmp()
    flagged[0] = {"code": "2345-7", "value": 95, "flags": ["LIP"]}
    _enter(client, order["id"], flagged)                # interference hold
    r = client.get("/lab/autoverify/rates", params={"days": 30},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    chem = next(i for i in r.json()["items"] if i["bench"] == "chemistry")
    assert chem["released"] == 2 and chem["held"] == 2
    assert chem["total"] == 4 and chem["rate"] == 0.5
    haem = next(i for i in r.json()["items"] if i["bench"] == "haematology")
    assert haem["total"] == 0 and haem["rate"] is None  # no fake rate
