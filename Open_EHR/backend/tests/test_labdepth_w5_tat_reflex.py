"""Lab Depth W5 (LAB-D10 + LAB-D11) — TAT targets & escalation,
the critical-value clock, and same-specimen reflex depth.

Covers: breach math on each DECLARED clock separately (the same
order, the same stop-time — different verdicts per clock, because
order->result and receipt->result declare different starts and
different targets); the governed approaching-breach threshold;
escalation firing once per level in chain order (bench ->
supervisor -> lab manager); the critical clock starting at
release, escalating at the governed window, and stopping the
moment notification (with read-back) lands; reflex firing onto
the SAME specimen with volume consumed through the aliquot /
add-on machinery; insufficient volume refused BY NAME with a
recollect path; a stepwise A -> B -> C cascade with trigger
evidence per step; qualitative and quantitative-threshold
triggers; the reflex-billing flag; and an unmonitored test (no
published TAT target) handled honestly on the board.

All fixture times derive from UTC now (relative timestamps are
written onto the FHIR rows after creation — no wall-clock
expectations are hard-coded). Synthetic data only. The router is
mounted here when app.main has not wired it yet (the coordinator
wires it via the W5 INTEGRATION NOTE); the mount is skipped when
already wired.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import lab as lab_mod
from app import lab_tat as lab_tat_mod
from app.configstore import published_config
from app.main import create_app
from app.models import FhirResource, StudioConfig
from tests.conftest import CLINICIAN

GLUCOSE = "2345-7"      # _std_tat: routine order->result 240, receipt->result 180
TSH = "3016-3"          # low 0.4 — seeded reflex tsh-low-add-ft4 -> 3024-7
FT4 = "3024-7"
POTASSIUM = "2823-3"    # critical_high 6.0
HIV_SCREEN = "HIV-AGAB"
HIV_DIFF = "HIV-DIFF"
HIV_VL = "20447-9"


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/w5.db")
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/lab/tat/board" not in paths:
        app.include_router(lab_tat_mod.router)
    with TestClient(app) as c:
        with app.state.session_factory() as session:
            lab_tat_mod.ensure_lab_tat_defaults(session)
        yield c


def _now():
    return datetime.now(timezone.utc)


def _patient(client, pid):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic",
                                    "given": ["W5"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _order(client, code, patient, priority=None):
    body = {"code": code, "subject": {"reference": f"Patient/{patient}"}}
    if priority:
        body["priority"] = priority
    r = client.post("/lab/orders", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _transition(client, order_id, to_status):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": to_status}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _received(client, order_id):
    _transition(client, order_id, "collected")
    return _transition(client, order_id, "received")


def _results(client, order_id, results, expect=201):
    r = client.post(f"/lab/orders/{order_id}/results",
                    json={"results": results}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _mutate(client, fn):
    """Run fn(session) against the test app's database."""
    with client.app.state.session_factory() as session:
        fn(session)
        session.commit()


def _set_order_times(client, order_id, authored=None, received=None):
    def fn(session):
        order = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "ServiceRequest",
            FhirResource.resource_id == order_id)).first()
        body = dict(order.body or {})
        if authored is not None:
            body["authoredOn"] = authored.isoformat()
            order.body = body
        if received is not None:
            specimen = lab_mod._specimen_row(session, order)
            sbody = dict(specimen.body or {})
            sbody["receivedDateTime"] = received.isoformat()
            specimen.body = sbody
    _mutate(client, fn)


def _set_report_issued(client, report_id, issued):
    def fn(session):
        report = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "DiagnosticReport",
            FhirResource.resource_id == report_id)).first()
        body = dict(report.body or {})
        body["issued"] = issued.isoformat()
        report.body = body
    _mutate(client, fn)


def _add_reflex_rule(client, rule):
    def fn(session):
        cfg = published_config(session, "terminology",
                               lab_mod.LAB_CATALOG_KEY)
        payload = copy.deepcopy(cfg.payload)
        payload["reflexRules"] = list(payload.get("reflexRules") or []) \
            + [rule]
        cfg.payload = payload
    _mutate(client, fn)


def _board(client):
    r = client.get("/lab/tat/board", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _bench(board, code):
    return next(b for b in board["benches"] if b["bench"] == code)


def _entries(board, bench, order_id):
    b = _bench(board, bench)
    out = []
    for key in ("approaching", "breached", "awaiting_receipt"):
        out.extend(e for e in b[key] if e["order_id"] == order_id)
    return out


def _scan(client):
    r = client.post("/lab/tat/scan", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _escalations(client):
    r = client.get("/lab/tat/escalations", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _firings(client):
    r = client.get("/lab/tat/reflex", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _release(client, report_id):
    r = client.post(f"/lab/reports/{report_id}/verify",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/reports/{report_id}/release",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# D10 — declared clocks: breach math per clock, separately
# ---------------------------------------------------------------------------

def test_tat_clocks_declared_separately_different_verdicts(client):
    _patient(client, "pat-w5-1")
    order_id = _order(client, GLUCOSE, "pat-w5-1", priority="routine")
    _received(client, order_id)
    now = _now()
    # Ordered 200 min ago, received 10 min later (190 min ago).
    # order->result target 240: 200 elapsed = approaching, NOT breached.
    # receipt->result target 180: 190 elapsed = BREACHED.
    # Same order, same stop-time (now) — different verdicts per clock.
    _set_order_times(client, order_id,
                     authored=now - timedelta(minutes=200),
                     received=now - timedelta(minutes=190))
    board = _board(client)
    entries = {e["clock"]: e for e in _entries(board, "chemistry", order_id)}
    assert set(entries) == {"order-to-result", "receipt-to-result"}
    order_clock = entries["order-to-result"]
    assert order_clock["target_minutes"] == 240
    assert order_clock["state"] == "approaching"
    assert order_clock["clock_label"] == "order → result"
    assert order_clock["elapsed_minutes"] == pytest.approx(200, abs=2)
    receipt_clock = entries["receipt-to-result"]
    assert receipt_clock["target_minutes"] == 180
    assert receipt_clock["state"] == "breached"
    assert receipt_clock["clock_label"] == "receipt → result"
    assert receipt_clock["minutes_past_breach"] == pytest.approx(10, abs=2)
    # The breached list carries the receipt clock entry ONLY — the
    # order clock entry sits in approaching, never blended.
    bench = _bench(board, "chemistry")
    assert [e["clock"] for e in bench["breached"]
            if e["order_id"] == order_id] == ["receipt-to-result"]
    assert [e["clock"] for e in bench["approaching"]
            if e["order_id"] == order_id] == ["order-to-result"]


def test_approaching_breach_threshold_is_governed_fraction(client):
    _patient(client, "pat-w5-2")
    # Warning fraction 0.8 of the 240-min order clock = 192 min.
    near = _order(client, GLUCOSE, "pat-w5-2", priority="routine")
    _received(client, near)
    far = _order(client, GLUCOSE, "pat-w5-2", priority="routine")
    _received(client, far)
    now = _now()
    _set_order_times(client, near, authored=now - timedelta(minutes=195))
    _set_order_times(client, far, authored=now - timedelta(minutes=100))
    board = _board(client)
    bench = _bench(board, "chemistry")
    approaching_ids = {e["order_id"] for e in bench["approaching"]}
    assert near in approaching_ids          # 195 >= 192: approaching
    assert far not in approaching_ids       # 100 < 192: within target
    assert board["warning_fraction"] == 0.8


def test_unmonitored_test_named_honestly(client):
    _patient(client, "pat-w5-3")
    # Glucose publishes targets for stat + routine only: an order at
    # priority "asap" has NO governed target and is unmonitored.
    order_id = _order(client, GLUCOSE, "pat-w5-3", priority="asap")
    board = _board(client)
    unmonitored = [u for u in board["unmonitored"]
                   if u["order_id"] == order_id]
    assert len(unmonitored) == 1
    assert unmonitored[0]["monitored"] is False
    assert "none for priority 'asap'" in unmonitored[0]["reason"]
    # And it appears on NO bench's breach lists.
    for bench in board["benches"]:
        assert all(e["order_id"] != order_id
                   for e in bench["approaching"] + bench["breached"])


# ---------------------------------------------------------------------------
# D10 — escalation chain: once per level, in order
# ---------------------------------------------------------------------------

def test_escalation_fires_once_per_level_in_order(client):
    _patient(client, "pat-w5-4")
    deep = _order(client, GLUCOSE, "pat-w5-4", priority="routine")
    _received(client, deep)
    shallow = _order(client, GLUCOSE, "pat-w5-4", priority="routine")
    _received(client, shallow)
    now = _now()
    # Deep: 110 min past the receipt-clock breach -> both levels due
    # (supervisor at breach +0, manager at breach +60).
    _set_order_times(client, deep,
                     authored=now - timedelta(minutes=300),
                     received=now - timedelta(minutes=290))
    # Shallow: 10 min past breach -> supervisor only.
    _set_order_times(client, shallow,
                     authored=now - timedelta(minutes=255),
                     received=now - timedelta(minutes=190))
    first = _scan(client)
    assert first["fired_count"] == 3
    items = _escalations(client)
    deep_items = [i for i in items if i["subject_key"] == deep]
    assert [i["level"] for i in deep_items] == [1, 2]  # chain order
    assert deep_items[0]["target_role"] == "bench-supervisor"
    assert deep_items[0]["target_staff_id"] == "staff-201"
    assert deep_items[1]["target_role"] == "lab-manager"
    assert deep_items[1]["level_name"] == "lab-manager"
    assert all(i["task_id"] for i in deep_items)
    shallow_items = [i for i in items if i["subject_key"] == shallow]
    assert [i["level"] for i in shallow_items] == [1]
    # No duplicate spam: a second scan fires nothing new.
    second = _scan(client)
    assert second["fired_count"] == 0
    assert len(_escalations(client)) == 3


# ---------------------------------------------------------------------------
# D10 — critical-value clock inside the lab
# ---------------------------------------------------------------------------

def test_critical_clock_starts_at_release_escalates_stops_on_notify(client):
    _patient(client, "pat-w5-5")
    order_id = _order(client, POTASSIUM, "pat-w5-5")
    _received(client, order_id)
    outcome = _results(client, order_id,
                       [{"code": POTASSIUM, "value": 6.5}])  # > critical 6.0
    report_id = outcome["report"]["id"]
    # Verified but NOT released: the chase has not started.
    r = client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    chase = client.get("/lab/tat/critical-chase",
                       headers=CLINICIAN).json()
    assert all(i["report_id"] != report_id for i in chase["items"])
    # Release, then backdate the release stamp 45 min: past the
    # governed 30-min notify window by 15 -> supervisor level due,
    # manager level (window + 30) not yet.
    r = client.post(f"/lab/reports/{report_id}/release", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    _set_report_issued(client, report_id, _now() - timedelta(minutes=45))
    chase = client.get("/lab/tat/critical-chase",
                       headers=CLINICIAN).json()
    item = next(i for i in chase["items"] if i["report_id"] == report_id)
    assert item["chase_state"] == "breached"
    assert item["notify_window_minutes"] == 30
    assert item["minutes_past_window"] == pytest.approx(15, abs=2)
    assert item["notification"]["state"] == "pending"
    fired = _scan(client)
    crit = [f for f in fired["fired"] if f["kind"] == "critical"
            and f["subject_key"] == report_id]
    assert [f["level"] for f in crit] == [1]
    # Age the release to 75 min: manager level now due.
    _set_report_issued(client, report_id, _now() - timedelta(minutes=75))
    fired = _scan(client)
    crit = [f for f in fired["fired"] if f["kind"] == "critical"
            and f["subject_key"] == report_id]
    assert [f["level"] for f in crit] == [2]
    # Notification (with read-back, the existing machinery) stops
    # the chase: state flips to notified and no scan fires again.
    r = client.post(f"/lab/reports/{report_id}/notify",
                    json={"notified_to": "Dr Synthetic (ward)",
                          "read_back": True}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    chase = client.get("/lab/tat/critical-chase",
                       headers=CLINICIAN).json()
    item = next(i for i in chase["items"] if i["report_id"] == report_id)
    assert item["chase_state"] == "notified"
    assert item["notification"]["state"] == "notified"
    assert _scan(client)["fired_count"] == 0


# ---------------------------------------------------------------------------
# D11 — same-specimen reflex
# ---------------------------------------------------------------------------

def _specimen_id(client, order_id):
    r = client.get(f"/lab/orders/{order_id}", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()["specimen"]["id"]


def _chain(client, specimen_id):
    r = client.get(f"/lab/specimens/{specimen_id}/chain",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _specimen_count(client):
    r = client.get("/lab/specimens", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()["total"]


def test_reflex_fires_onto_same_specimen_volume_decremented(client):
    _patient(client, "pat-w5-6")
    order_id = _order(client, TSH, "pat-w5-6")
    _received(client, order_id)
    specimen_id = _specimen_id(client, order_id)
    before = _specimen_count(client)
    outcome = _results(client, order_id, [{"code": TSH, "value": 0.2}])
    assert len(outcome["reflex_orders"]) == 1
    reflex_order_id = outcome["reflex_orders"][0]
    # SAME specimen: no new specimen was minted, and the reflex
    # order links the source specimen.
    assert _specimen_count(client) == before
    detail = client.get(f"/lab/orders/{reflex_order_id}",
                        headers=CLINICIAN).json()
    assert detail["specimen"]["id"] == specimen_id
    # Volume consumed through the add-on machinery: the node's
    # remaining volume drops by the add-on's required volume (the
    # aliquot-rules default, 5.0 mL, of the 10.0 mL node).
    chain = _chain(client, specimen_id)
    root = chain["tree"]
    assert root["remaining_volume_ml"] == pytest.approx(5.0)
    firing = next(f for f in _firings(client)
                  if f["addon_order_id"] == reflex_order_id)
    assert firing["status"] == "fired"
    assert firing["rule_id"] == "tsh-low-add-ft4"
    assert firing["specimen_id"] == specimen_id
    assert firing["required_volume_ml"] == pytest.approx(5.0)
    assert firing["trigger_code"] == TSH
    assert firing["trigger_value"] == "0.2"
    assert firing["trigger_kind"] == "quantitative"
    assert firing["cascade_step"] == 1


def test_reflex_billing_flag_present(client):
    _patient(client, "pat-w5-7")
    order_id = _order(client, TSH, "pat-w5-7")
    _received(client, order_id)
    outcome = _results(client, order_id, [{"code": TSH, "value": 0.2}])
    reflex_order_id = outcome["reflex_orders"][0]
    orders = client.get("/lab/orders", params={"status": "ordered"},
                        headers=CLINICIAN).json()
    resource = next(e["resource"] for e in orders["entry"]
                    if e["resource"]["id"] == reflex_order_id)
    assert resource["reflexBilling"] is True
    ext = {e["url"]: e for e in resource.get("extension") or []}
    assert ext["urn:opencare:extension/reflex-billing"]["valueBoolean"] \
        is True
    firing = next(f for f in _firings(client)
                  if f["addon_order_id"] == reflex_order_id)
    assert firing["reflex_billing"] is True


def test_reflex_insufficient_volume_refused_by_name_recollect(client):
    _patient(client, "pat-w5-8")
    order_id = _order(client, TSH, "pat-w5-8")
    _received(client, order_id)
    specimen_id = _specimen_id(client, order_id)
    _chain(client, specimen_id)  # materialise the lifecycle node
    # Consume 6 of the node's 10 mL through the aliquot machinery,
    # leaving 4 mL — less than the 5 mL the FT4 add-on requires.
    r = client.post(f"/lab/specimens/{specimen_id}/derive",
                    json={"type": "aliquot", "count": 1,
                          "volume_ml": 6.0}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    outcome = _results(client, order_id, [{"code": TSH, "value": 0.2}])
    # Named refusal, NO fresh order minted.
    assert outcome["reflex_orders"] == []
    firing = next(f for f in _firings(client)
                  if f["source_order_id"] == order_id)
    assert firing["status"] == "refused-insufficient-volume"
    assert "Insufficient volume" in firing["refusal_reason"]
    assert FT4 in firing["refusal_reason"]
    assert firing["remaining_before_ml"] == pytest.approx(4.0)
    assert firing["addon_order_id"] is None
    # Recollect path: a recollect Task on the source order.
    r = client.post(
        f"/lab/tat/reflex-firings/{firing['firing_id']}/request-recollect",
        json={}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["task_id"]
    assert r.json()["firing"]["status"] == "recollect-requested"
    r = client.post(
        f"/lab/tat/reflex-firings/{firing['firing_id']}/request-recollect",
        json={}, headers=CLINICIAN)
    assert r.status_code == 409, r.text


def test_reflex_qualitative_trigger(client):
    _patient(client, "pat-w5-9")
    order_id = _order(client, HIV_SCREEN, "pat-w5-9")
    _received(client, order_id)
    outcome = _results(client, order_id,
                       [{"code": HIV_SCREEN, "value": "Reactive"}])
    assert len(outcome["reflex_orders"]) == 1
    firing = next(f for f in _firings(client)
                  if f["source_order_id"] == order_id)
    assert firing["rule_id"] == "hiv-reactive-add-diff"
    assert firing["trigger_kind"] == "qualitative"
    assert firing["trigger_value"] == "Reactive"
    assert firing["add_code"] == HIV_DIFF
    assert firing["status"] == "fired"


def test_reflex_cascade_a_b_c_stepwise_with_evidence(client):
    _add_reflex_rule(client, {
        "id": "hiv-diff-add-vl", "triggerCode": HIV_DIFF,
        "when": "qualitative-match", "matchValue": "HIV-1 positive",
        "addCode": HIV_VL,
        "note": "Confirmed HIV-1 reflexes the viral load (test rule)"})
    _patient(client, "pat-w5-10")
    order_id = _order(client, HIV_SCREEN, "pat-w5-10")
    _received(client, order_id)
    specimen_id = _specimen_id(client, order_id)
    # Step A -> B: Reactive screen reflexes the differentiation.
    outcome = _results(client, order_id,
                       [{"code": HIV_SCREEN, "value": "Reactive"}])
    step1 = next(f for f in _firings(client)
                 if f["rule_id"] == "hiv-reactive-add-diff")
    assert step1["cascade_step"] == 1
    assert step1["specimen_id"] == specimen_id
    # Begin bench work on B (the specimen is already received).
    r = client.post(
        f"/lab/tat/reflex-firings/{step1['firing_id']}/begin",
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["order"]["status"] == "received"
    # Step B -> C: the differentiation result fires the viral load
    # against the SAME specimen, evidence chained to step 1.
    outcome2 = _results(client, step1["addon_order_id"],
                        [{"code": HIV_DIFF, "value": "HIV-1 positive"}])
    assert len(outcome2["reflex_orders"]) == 1
    step2 = next(f for f in _firings(client)
                 if f["rule_id"] == "hiv-diff-add-vl")
    assert step2["cascade_step"] == 2
    assert step2["parent_firing_id"] == step1["firing_id"]
    assert step2["trigger_value"] == "HIV-1 positive"
    assert step2["specimen_id"] == specimen_id
    detail = client.get(f"/lab/orders/{outcome2['reflex_orders'][0]}",
                        headers=CLINICIAN).json()
    assert detail["specimen"]["id"] == specimen_id
    # Two add-ons consumed 5 mL each from the one 10 mL specimen.
    chain = _chain(client, specimen_id)
    assert chain["tree"]["remaining_volume_ml"] == pytest.approx(0.0)


def test_reflex_quantitative_threshold_rule(client):
    _add_reflex_rule(client, {
        "id": "tsh-high-add-ft4", "triggerCode": TSH,
        "when": "above-value", "value": 10, "addCode": FT4,
        "note": "TSH above the governed threshold reflexes free T4"})
    _patient(client, "pat-w5-11")
    order_id = _order(client, TSH, "pat-w5-11")
    _received(client, order_id)
    outcome = _results(client, order_id, [{"code": TSH, "value": 12.0}])
    assert len(outcome["reflex_orders"]) == 1
    firing = next(f for f in _firings(client)
                  if f["source_order_id"] == order_id)
    assert firing["rule_id"] == "tsh-high-add-ft4"
    assert firing["trigger_kind"] == "quantitative"
    assert firing["trigger_value"] == "12.0"
    # Below the threshold nothing fires.
    order2 = _order(client, TSH, "pat-w5-11")
    _received(client, order2)
    outcome2 = _results(client, order2, [{"code": TSH, "value": 9.0}])
    assert outcome2["reflex_orders"] == []
