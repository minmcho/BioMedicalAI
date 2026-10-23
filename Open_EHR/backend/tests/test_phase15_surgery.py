"""Phase 15 (E) — surgery department depth.

Covers: seeded Studio config + the governed-append PACU gate flag;
case requests + waitlist ordering; request scheduling into a real
boards case with the in-block flag REPORTED; block utilisation exact
math; preference cards + FEFO implant pick lists (oversell 409);
sterile processing lifecycle + cycle validation + case assignment;
intra-op governed forms; the PACU gate flip-proven ON and OFF;
outcomes + the SSI surveillance feed.

Dates: 2026-10-19 is a Monday (the seeded dr-smith OR-1 block is
Monday 08:00-16:00), 2026-10-20 a Tuesday.
"""
from datetime import datetime, timedelta, timezone

from .conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

MON = "2026-10-19"
TUE = "2026-10-20"

TIMEOUT = ["timeout-introductions", "timeout-verbal-confirmation",
           "timeout-antibiotics", "timeout-imaging", "timeout-concerns"]
SIGNOUT = ["signout-procedure-recorded", "signout-counts",
           "signout-specimen", "signout-recovery"]
PASSING_SCORES = {"activity": 2, "respiration": 2, "circulation": 2,
                  "consciousness": 2, "oxygen-saturation": 1}  # total 9


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Surg", "given": ["Test"]}],
        "birthDate": "1980-01-01", "gender": "female"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _case(client, pid, code="27447", surgeon="dr-smith", room="OR-1",
          day=MON, start="08:00", end="10:00"):
    body = {"patient_id": pid, "code": code,
            "display": "Total knee arthroplasty", "room": room,
            "start": f"{day}T{start}:00Z", "end": f"{day}T{end}:00Z"}
    if surgeon:
        body["surgeon"] = surgeon
    r = client.post("/surgery/cases", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["case"]["id"]


def _transition(client, case_id, target):
    return client.post(f"/surgery/cases/{case_id}/transition",
                       json={"to_status": target}, headers=CLINICIAN)


def _safety(client, case_id, section, codes):
    r = client.post(f"/surgery/cases/{case_id}/safety-checklist",
                    json={"section": section,
                          "answers": [{"linkId": c,
                                       "answer": [{"valueBoolean": True}]}
                                      for c in codes]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _to_pacu(client, case_id):
    assert _transition(client, case_id, "pre-op").status_code == 200
    _safety(client, case_id, "time-out", TIMEOUT)
    assert _transition(client, case_id, "in-op").status_code == 200
    assert _transition(client, case_id, "pacu").status_code == 200
    _safety(client, case_id, "sign-out", SIGNOUT)


def _publish_surgery_flow(client, mutate):
    r = client.get("/studio/config/workflow/effective/surgery-flow",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    mutate(payload)
    r = client.post("/studio/config/workflow/surgery-flow",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/workflow/surgery-flow/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


def _request(client, pid, urgency, surgeon="dr-smith", code="27447"):
    r = client.post("/surgery/requests", json={
        "patient_id": pid, "procedure_code": code,
        "procedure_display": "Total knee arthroplasty",
        "surgeon": surgeon, "urgency": urgency,
        "requested_start": "2026-11-02T08:00:00Z",
        "requested_end": "2026-11-02T12:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Seeded config + governed-append gate flag
# ---------------------------------------------------------------------------

def test_seeded_config_and_pacu_gate_appended(client):
    for kind, key in (("terminology", "surgery-urgency"),
                      ("terminology", "or-blocks"),
                      ("terminology", "instrument-trays"),
                      ("terminology", "sterile-processing"),
                      ("terminology", "pacu-scoring"),
                      ("terminology", "surgery-complications"),
                      ("form", "intraop-counts"),
                      ("form", "intraop-positioning"),
                      ("form", "intraop-specimens")):
        r = client.get(f"/studio/config/{kind}/{key}", headers=ADMIN)
        assert r.status_code == 200, (kind, key, r.text)
        assert any(row["status"] == "published" for row in r.json()), (kind, key)
    # The PACU gate flag was governed-appended onto the published
    # surgery workflow WITHOUT disturbing the existing content.
    eff = client.get("/studio/config/workflow/effective/surgery-flow",
                     headers=CLINICIAN).json()
    assert eff["payload"]["pacuScoreGate"] is True
    assert eff["payload"]["safetyChecklistGate"] is True  # Phase 4B seed kept
    assert "pacu" in eff["payload"]["states"]


# ---------------------------------------------------------------------------
# 1) Case requests + waitlist
# ---------------------------------------------------------------------------

def test_request_lifecycle_and_waitlist_order(client):
    _patient(client, "sd-pat-1")
    elective = _request(client, "sd-pat-1", "elective")
    emergent = _request(client, "sd-pat-1", "emergent")
    urgent = _request(client, "sd-pat-1", "urgent")
    assert elective["status"] == "requested"
    assert elective["urgency_rank"] == 3 and emergent["urgency_rank"] == 1

    # Unknown urgency / unknown patient are refused up front.
    r = client.post("/surgery/requests", json={
        "patient_id": "sd-pat-1", "procedure_code": "27447",
        "surgeon": "dr-smith", "urgency": "whenever"}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/surgery/requests", json={
        "patient_id": "nobody", "procedure_code": "27447",
        "surgeon": "dr-smith", "urgency": "urgent"}, headers=CLINICIAN)
    assert r.status_code == 404

    # Decline requires a reason.
    r = client.post(f"/surgery/requests/{elective['request_id']}/decline",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post(f"/surgery/requests/{elective['request_id']}/decline",
                    json={"reason": "Not clinically indicated"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "declined"
    assert r.json()["decline_reason"] == "Not clinically indicated"
    # A decided request cannot be decided again.
    r = client.post(f"/surgery/requests/{elective['request_id']}/approve",
                    headers=CLINICIAN)
    assert r.status_code == 409

    for req in (emergent, urgent):
        r = client.post(f"/surgery/requests/{req['request_id']}/approve",
                        headers=CLINICIAN)
        assert r.status_code == 200, r.text
    waitlist = client.get("/surgery/waitlist", headers=CLINICIAN).json()
    assert waitlist["total"] == 2
    # Urgency rank first (emergent before urgent), waits computed.
    assert [i["request_id"] for i in waitlist["items"]] == \
        [emergent["request_id"], urgent["request_id"]]
    assert waitlist["items"][0]["wait_minutes"] >= 0
    assert waitlist["items"][0]["status"] == "approved"
    # Capability gates.
    assert client.get("/surgery/waitlist", headers=PATIENT).status_code == 403
    assert client.post("/surgery/requests", json={}, headers=VIEWER).status_code == 403


def test_schedule_request_creates_real_case_and_reports_block(client):
    _patient(client, "sd-pat-2")
    req = _request(client, "sd-pat-2", "urgent", surgeon="dr-smith")
    # Unapproved requests cannot be scheduled.
    r = client.post(f"/surgery/requests/{req['request_id']}/schedule",
                    json={"room": "OR-1", "start": f"{MON}T09:00:00Z",
                          "end": f"{MON}T11:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 409
    client.post(f"/surgery/requests/{req['request_id']}/approve",
                headers=CLINICIAN)
    # Monday 09:00-11:00 in OR-1 is inside dr-smith's seeded block.
    r = client.post(f"/surgery/requests/{req['request_id']}/schedule",
                    json={"room": "OR-1", "start": f"{MON}T09:00:00Z",
                          "end": f"{MON}T11:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["in_block"] is True
    assert body["block_code"] == "blk-or1-mon-ortho"
    assert body["case"]["status"] == "scheduled"
    assert body["request"]["status"] == "scheduled"
    assert body["request"]["case_id"] == body["case"]["id"]
    # The case is a REAL boards case: it shows on the surgery board.
    board = client.get("/surgery/board", params={"date": MON},
                       headers=CLINICIAN).json()
    ids = [e["case"]["id"] for room in board["rooms"] for e in room["cases"]]
    assert body["case"]["id"] in ids
    # ... and the room-overlap rule applies to request scheduling too.
    req2 = _request(client, "sd-pat-2", "elective", surgeon="dr-jones")
    client.post(f"/surgery/requests/{req2['request_id']}/approve",
                headers=CLINICIAN)
    r = client.post(f"/surgery/requests/{req2['request_id']}/schedule",
                    json={"room": "OR-1", "start": f"{MON}T10:00:00Z",
                          "end": f"{MON}T12:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 409  # overlaps the 09:00-11:00 case in OR-1
    # Outside the block (evening) schedules fine — reported, not blocked.
    r = client.post(f"/surgery/requests/{req2['request_id']}/schedule",
                    json={"room": "OR-1", "start": f"{MON}T18:00:00Z",
                          "end": f"{MON}T19:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["in_block"] is False
    assert r.json()["block_code"] is None
    # Scheduled requests leave the waitlist.
    waitlist = client.get("/surgery/waitlist", headers=CLINICIAN).json()
    assert waitlist["total"] == 0


# ---------------------------------------------------------------------------
# 2) Block utilisation — exact math
# ---------------------------------------------------------------------------

def test_utilisation_exact_math(client):
    _patient(client, "sd-pat-3")
    _case(client, "sd-pat-3", room="OR-1", start="08:00", end="10:00")   # 120
    _case(client, "sd-pat-3", room="OR-1", start="10:30", end="12:00")   # 90
    _case(client, "sd-pat-3", room="OR-2", start="09:00", end="10:00")   # 60
    r = client.get("/surgery/utilisation", params={"date": MON},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["weekday"] == "monday"
    rooms = {room["room"]: room for room in body["rooms"]}
    or1 = rooms["OR-1"]
    assert or1["scheduled_minutes"] == 210
    assert or1["block_minutes"] == 480          # Mon 08:00-16:00
    assert or1["in_block_minutes"] == 210
    assert or1["non_block_minutes"] == 0
    assert or1["utilisation_pct"] == 43.75      # 210/480 exactly
    assert len(or1["blocks"]) == 1              # Wednesday block excluded
    assert or1["blocks"][0]["block_minutes"] == 480
    assert or1["blocks"][0]["scheduled_minutes"] == 210
    assert or1["turnover"]["count"] == 1
    assert or1["turnover"]["total_gap_minutes"] == 30   # 10:00 -> 10:30
    assert or1["turnover"]["gaps"][0]["minutes"] == 30
    or2 = rooms["OR-2"]
    assert or2["scheduled_minutes"] == 60
    assert or2["block_minutes"] == 0            # dr-jones block is Tuesday
    assert or2["in_block_minutes"] == 0
    assert or2["non_block_minutes"] == 60
    assert or2["utilisation_pct"] is None
    assert client.get("/surgery/utilisation", params={"date": "19/10/2026"},
                      headers=CLINICIAN).status_code == 422


# ---------------------------------------------------------------------------
# 3) Preference cards + implant pick lists (FEFO / oversell)
# ---------------------------------------------------------------------------

def _receive_implant(client, lot, expiry, qty, serial=None):
    body = {"item_code": "implant-knee", "display": "Knee implant",
            "lot_number": lot, "expiry_date": expiry, "quantity": qty}
    if serial:
        body["serial"] = serial
    r = client.post("/surgery/implants/lots", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _card(client, surgeon="dr-smith", code="27447", implant_qty=2):
    r = client.post("/surgery/preference-cards", json={
        "surgeon": surgeon, "procedure_code": code,
        "items": [
            {"kind": "supply", "code": "gauze-pack", "qty": 4},
            {"kind": "implant", "code": "implant-knee", "qty": implant_qty},
            {"kind": "instrument-tray", "code": "major-orthopaedic", "qty": 1},
        ]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def test_preference_cards_and_fefo_pick_list(client):
    _patient(client, "sd-pat-4")
    card = _card(client)
    # One card per (surgeon, procedure): a second create is a 409.
    r = client.post("/surgery/preference-cards", json={
        "surgeon": "dr-smith", "procedure_code": "27447",
        "items": [{"kind": "supply", "code": "x", "qty": 1}]},
        headers=CLINICIAN)
    assert r.status_code == 409
    # Item kinds are a closed vocabulary.
    r = client.post("/surgery/preference-cards", json={
        "surgeon": "dr-x", "procedure_code": "1",
        "items": [{"kind": "magic", "code": "x", "qty": 1}]},
        headers=CLINICIAN)
    assert r.status_code == 422
    # CRUD: update replaces the items; read-back shows them.
    r = client.put(f"/surgery/preference-cards/{card['card_id']}", json={
        "items": [{"kind": "supply", "code": "gauze-pack", "qty": 6},
                  {"kind": "implant", "code": "implant-knee", "qty": 2},
                  {"kind": "instrument-tray", "code": "major-orthopaedic",
                   "qty": 1}]}, headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["items"][0]["qty"] == 6

    # Stock: FEFO must take the Dec-2026 lot before the Jun-2027 lot;
    # the expired lot never allocates.
    _receive_implant(client, "LOT-LATE", "2027-06-01", 1, serial="SN-LATE")
    _receive_implant(client, "LOT-EARLY", "2026-12-01", 1, serial="SN-EARLY")
    _receive_implant(client, "LOT-OLD", "2020-01-01", 5)
    # Same lot + different expiry is refused (pharmacy receiving rule).
    r = client.post("/surgery/implants/lots", json={
        "item_code": "implant-knee", "lot_number": "LOT-EARLY",
        "expiry_date": "2028-01-01", "quantity": 1}, headers=CLINICIAN)
    assert r.status_code == 422

    case_id = _case(client, "sd-pat-4")
    r = client.post(f"/surgery/cases/{case_id}/pick-list", headers=CLINICIAN)
    assert r.status_code == 201, r.text
    pick = r.json()
    implant = next(i for i in pick["items"] if i["kind"] == "implant")
    assert [a["lot_number"] for a in implant["allocations"]] == \
        ["LOT-EARLY", "LOT-LATE"]                     # FEFO order
    assert implant["allocations"][0]["serial"] == "SN-EARLY"
    lots = {l["lot_number"]: l for l in
            client.get("/surgery/implants/lots", headers=CLINICIAN).json()["items"]}
    assert lots["LOT-EARLY"]["quantity"] == 0
    assert lots["LOT-LATE"]["quantity"] == 0
    assert lots["LOT-OLD"]["quantity"] == 5           # expired lot untouched
    # The pick is recorded against the case + traced.
    picks = client.get(f"/surgery/cases/{case_id}/picks",
                       headers=CLINICIAN).json()
    assert picks["total"] == 1
    trace = client.get("/surgery/implants/trace", headers=CLINICIAN).json()
    pick_events = [e for e in trace["items"] if e["event"] == "pick"]
    assert len(pick_events) == 2
    assert all(e["reference"] == f"Procedure/{case_id}" for e in pick_events)
    record = client.get(f"/surgery/cases/{case_id}/record",
                        headers=CLINICIAN).json()
    assert record["picks"][0]["pick_id"] == pick["pick_id"]


def test_pick_list_oversell_refused_atomically(client):
    _patient(client, "sd-pat-5")
    _card(client, surgeon="dr-jones", code="47562", implant_qty=3)
    _receive_implant(client, "LOT-ONLY", "2027-01-01", 2)
    case_id = _case(client, "sd-pat-5", code="47562", surgeon="dr-jones",
                    room="OR-2", day=TUE, start="08:00", end="09:00")
    r = client.post(f"/surgery/cases/{case_id}/pick-list", headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "oversell" in r.json()["detail"]
    # Nothing moved: the lot is intact and no pick was recorded.
    lots = client.get("/surgery/implants/lots", headers=CLINICIAN).json()
    assert lots["items"][0]["quantity"] == 2
    picks = client.get(f"/surgery/cases/{case_id}/picks",
                       headers=CLINICIAN).json()
    assert picks["total"] == 0
    # No card for this surgeon/procedure -> 404.
    case2 = _case(client, "sd-pat-5", code="99999", surgeon="dr-nobody",
                  room="OR-3", day=TUE, start="08:00", end="09:00")
    r = client.post(f"/surgery/cases/{case2}/pick-list", headers=CLINICIAN)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 4) Sterile processing
# ---------------------------------------------------------------------------

def _walk_tray(client, tray_id, targets):
    for target in targets:
        r = client.post(f"/surgery/trays/{tray_id}/transition",
                        json={"to_status": target}, headers=CLINICIAN)
        assert r.status_code == 200, (target, r.text)


def _sterilise(client, tray_id):
    _walk_tray(client, tray_id, ["decontamination", "assembly", "sterilising"])
    r = client.post(f"/surgery/trays/{tray_id}/cycles", json={
        "cycle_type": "steam-134", "temperature_c": 134,
        "duration_minutes": 20, "indicator_result": "pass"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def test_sterile_processing_lifecycle_and_assignment(client, tmp_path):
    _patient(client, "sd-pat-6")
    case_id = _case(client, "sd-pat-6")
    r = client.post("/surgery/trays", json={
        "tray_id": "TRAY-1", "tray_type": "major-orthopaedic"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "in-use"
    # Unknown tray type / skipping a lifecycle step are refused.
    r = client.post("/surgery/trays", json={"tray_type": "bogus"},
                    headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/surgery/trays/TRAY-1/transition",
                    json={"to_status": "assembly"}, headers=CLINICIAN)
    assert r.status_code == 409
    # A non-sterile tray cannot be issued.
    r = client.post(f"/surgery/cases/{case_id}/trays",
                    json={"tray_id": "TRAY-1"}, headers=CLINICIAN)
    assert r.status_code == 409 and "in-use" in r.json()["detail"]

    _walk_tray(client, "TRAY-1", ["decontamination", "assembly", "sterilising"])
    # sterilising -> sterile is governed: only a cycle gets there.
    r = client.post("/surgery/trays/TRAY-1/transition",
                    json={"to_status": "sterile"}, headers=CLINICIAN)
    assert r.status_code == 409 and "cycle" in r.json()["detail"]
    # Cycle parameters validate against the CONFIG cycle types:
    # steam-134 needs >=134C and >=18 min, and an indicator result.
    r = client.post("/surgery/trays/TRAY-1/cycles", json={
        "cycle_type": "steam-134", "temperature_c": 120,
        "duration_minutes": 20, "indicator_result": "pass"}, headers=CLINICIAN)
    assert r.status_code == 422 and "134" in r.json()["detail"]
    r = client.post("/surgery/trays/TRAY-1/cycles", json={
        "cycle_type": "steam-134", "temperature_c": 134,
        "duration_minutes": 20}, headers=CLINICIAN)
    assert r.status_code == 422 and "indicator" in r.json()["detail"]
    r = client.post("/surgery/trays/TRAY-1/cycles", json={
        "cycle_type": "bogus", "temperature_c": 134,
        "duration_minutes": 20, "indicator_result": "pass"}, headers=CLINICIAN)
    assert r.status_code == 422
    # A failed indicator sends the tray back to decontamination.
    r = client.post("/surgery/trays/TRAY-1/cycles", json={
        "cycle_type": "steam-134", "temperature_c": 134,
        "duration_minutes": 20, "indicator_result": "fail"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["tray"]["status"] == "decontamination"
    # Reprocess and pass: sterile, shelf life = config 30 days.
    _walk_tray(client, "TRAY-1", ["assembly", "sterilising"])
    r = client.post("/surgery/trays/TRAY-1/cycles", json={
        "cycle_type": "steam-134", "temperature_c": 135,
        "duration_minutes": 25, "indicator_result": "pass"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    tray = r.json()["tray"]
    assert tray["status"] == "sterile"
    sterilised = datetime.fromisoformat(tray["sterilised_at"])
    expires = datetime.fromisoformat(tray["expires_at"])
    assert expires - sterilised == timedelta(days=30)
    # Issue to the case: sterile -> issued, recorded on the record.
    r = client.post(f"/surgery/cases/{case_id}/trays",
                    json={"tray_id": "TRAY-1"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["tray"]["status"] == "issued"
    assert r.json()["tray"]["assigned_case_id"] == case_id
    record = client.get(f"/surgery/cases/{case_id}/record",
                        headers=CLINICIAN).json()
    assert [t["tray_id"] for t in record["trays"]] == ["TRAY-1"]

    # Shelf-life expiry refuses issue: backdate TRAY-2's expiry in the
    # test database (the tray was genuinely sterilised + issued via
    # the API above for TRAY-1; expiry itself cannot be time-travelled
    # through the API, by design).
    client.post("/surgery/trays", json={"tray_id": "TRAY-2",
                                        "tray_type": "minor-general"},
                headers=CLINICIAN)
    _sterilise(client, "TRAY-2")
    import glob
    import sqlite3
    db_files = glob.glob(str(tmp_path / "*.db")) + \
        glob.glob(str(tmp_path / "**" / "*.db"), recursive=True)
    assert db_files, "test database file not found"
    conn = sqlite3.connect(db_files[0])
    conn.execute("UPDATE surgery_tray_instances SET expires_at = ? "
                 "WHERE tray_id = 'TRAY-2'",
                 ("2020-01-01T00:00:00+00:00",))
    conn.commit()
    conn.close()
    r = client.post(f"/surgery/cases/{case_id}/trays",
                    json={"tray_id": "TRAY-2"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "shelf life" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 5) Intra-operative record
# ---------------------------------------------------------------------------

def test_intraop_forms_attach_to_case_record(client):
    _patient(client, "sd-pat-7")
    case_id = _case(client, "sd-pat-7")
    r = client.post(f"/surgery/cases/{case_id}/intraop/intraop-counts",
                    json={"answers": [
                        {"linkId": "sponge-count-correct",
                         "answer": [{"valueBoolean": True}]},
                        {"linkId": "sharps-count-correct",
                         "answer": [{"valueBoolean": True}]},
                        {"linkId": "instrument-count-correct",
                         "answer": [{"valueBoolean": False}]},
                        {"linkId": "sponges-used",
                         "answer": [{"valueInteger": 12}]},
                    ]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["response"]["questionnaire"] == "intraop-counts"
    r = client.post(f"/surgery/cases/{case_id}/intraop/intraop-positioning",
                    json={"answers": [
                        {"linkId": "position",
                         "answer": [{"valueCoding": {"code": "supine"}}]},
                        {"linkId": "pressure-areas-padded",
                         "answer": [{"valueBoolean": True}]},
                    ]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # Unknown form key -> 404; missing required answers -> 422 (the
    # Phase 5 validator inside the form machinery).
    r = client.post(f"/surgery/cases/{case_id}/intraop/intraop-bogus",
                    json={"answers": []}, headers=CLINICIAN)
    assert r.status_code == 404
    r = client.post(f"/surgery/cases/{case_id}/intraop/intraop-specimens",
                    json={"answers": []}, headers=CLINICIAN)
    assert r.status_code == 422
    record = client.get(f"/surgery/cases/{case_id}/record",
                        headers=CLINICIAN).json()
    assert set(record["intraop"]) == {"intraop-counts", "intraop-positioning"}
    assert record["intraop"]["intraop-counts"]["response"]["id"]


# ---------------------------------------------------------------------------
# 6) PACU scoring + gate (flip-proven both states)
# ---------------------------------------------------------------------------

def test_pacu_score_validation_and_computed_total(client):
    _patient(client, "sd-pat-8")
    case_id = _case(client, "sd-pat-8")
    # Unknown criterion / missing criterion / out-of-scale all 422.
    bad = dict(PASSING_SCORES)
    bad["luck"] = 2
    r = client.post(f"/surgery/cases/{case_id}/pacu-score",
                    json={"scores": bad}, headers=CLINICIAN)
    assert r.status_code == 422 and "luck" in r.json()["detail"]
    r = client.post(f"/surgery/cases/{case_id}/pacu-score",
                    json={"scores": {"activity": 2}}, headers=CLINICIAN)
    assert r.status_code == 422 and "respiration" in r.json()["detail"]
    bad = dict(PASSING_SCORES)
    bad["activity"] = 3
    r = client.post(f"/surgery/cases/{case_id}/pacu-score",
                    json={"scores": bad}, headers=CLINICIAN)
    assert r.status_code == 422
    # The total is computed server-side: 2+2+2+2+1 = 9 = threshold.
    r = client.post(f"/surgery/cases/{case_id}/pacu-score",
                    json={"scores": PASSING_SCORES, "total": 0},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["total"] == 9 and r.json()["threshold"] == 9
    assert r.json()["passes"] is True


def test_pacu_gate_on_refuses_done_until_passing_score(client):
    _patient(client, "sd-pat-9")
    case_id = _case(client, "sd-pat-9")
    _to_pacu(client, case_id)
    # Gate ON (seeded): no score -> 409 naming the missing score.
    r = _transition(client, case_id, "done")
    assert r.status_code == 409, r.text
    assert "PACU" in r.json()["detail"] and "no PACU score" in r.json()["detail"]
    # A below-threshold score -> 409 naming the score vs threshold.
    low = {"activity": 1, "respiration": 2, "circulation": 1,
           "consciousness": 2, "oxygen-saturation": 1}   # total 7
    r = client.post(f"/surgery/cases/{case_id}/pacu-score",
                    json={"scores": low}, headers=CLINICIAN)
    assert r.json()["total"] == 7 and r.json()["passes"] is False
    r = _transition(client, case_id, "done")
    assert r.status_code == 409
    assert "7" in r.json()["detail"] and "9" in r.json()["detail"]
    # A must-pass failure is recorded in the verdict (respiration 0
    # breaches the configured minimum 1) and the gate still refuses.
    scores = {"activity": 2, "respiration": 0, "circulation": 2,
              "consciousness": 2, "oxygen-saturation": 2}
    r = client.post(f"/surgery/cases/{case_id}/pacu-score",
                    json={"scores": scores}, headers=CLINICIAN)
    body = r.json()
    assert body["passes"] is False
    assert body["must_pass_failures"] and \
        body["must_pass_failures"][0]["code"] == "respiration"
    r = _transition(client, case_id, "done")
    assert r.status_code == 409
    # A passing score releases the transition.
    r = client.post(f"/surgery/cases/{case_id}/pacu-score",
                    json={"scores": PASSING_SCORES}, headers=CLINICIAN)
    assert r.json()["passes"] is True
    r = _transition(client, case_id, "done")
    assert r.status_code == 200, r.text
    assert r.json()["case"]["status"] == "done"


def test_pacu_gate_flip_off_restores_prior_behaviour(client):
    _publish_surgery_flow(client, lambda p: p.update(
        {"pacuScoreGate": False, "safetyChecklistGate": False}))
    _patient(client, "sd-pat-10")
    case_id = _case(client, "sd-pat-10")
    # No checklist, no PACU score: with both gates published OFF the
    # flow runs exactly as it did before the depth passes.
    for target in ("pre-op", "in-op", "pacu", "done"):
        r = _transition(client, case_id, target)
        assert r.status_code == 200, (target, r.text)
    record = client.get(f"/surgery/cases/{case_id}/record",
                        headers=CLINICIAN).json()
    assert record["pacu_gate_enabled"] is False


# ---------------------------------------------------------------------------
# 7) Outcomes + SSI surveillance feed
# ---------------------------------------------------------------------------

def test_outcome_validation_and_ssi_feed(client):
    _patient(client, "sd-pat-11")
    case_id = _case(client, "sd-pat-11")
    r = client.post(f"/surgery/cases/{case_id}/outcome", json={
        "status": "recovered", "complications": ["bogus-code"]},
        headers=CLINICIAN)
    assert r.status_code == 422 and "bogus-code" in r.json()["detail"]
    r = client.post(f"/surgery/cases/{case_id}/outcome", json={
        "status": "fine"}, headers=CLINICIAN)
    assert r.status_code == 422
    # SSI without an onset date is refused.
    r = client.post(f"/surgery/cases/{case_id}/outcome", json={
        "status": "complication", "ssi": True}, headers=CLINICIAN)
    assert r.status_code == 422 and "ssi_onset_date" in r.json()["detail"]
    # A clean outcome keeps the case off the feed.
    r = client.post(f"/surgery/cases/{case_id}/outcome", json={
        "status": "recovered", "complications": []}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    feed = client.get("/surgery/outcomes/ssi", headers=CLINICIAN).json()
    assert feed["total"] == 0
    assert "surveillance" in feed["feed"]
    # Correcting the outcome with an SSI puts the case on the feed.
    r = client.post(f"/surgery/cases/{case_id}/outcome", json={
        "status": "complication", "complications": ["wound-infection"],
        "ssi": True, "ssi_onset_date": "2026-10-26",
        "organism": "Staphylococcus aureus"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    feed = client.get("/surgery/outcomes/ssi", headers=CLINICIAN).json()
    assert feed["total"] == 1
    item = feed["items"][0]
    assert item["case_id"] == case_id
    assert item["organism"] == "Staphylococcus aureus"
    assert item["ssi_onset_date"] == "2026-10-26"
    assert item["procedure_code"] == "27447"
    assert "infection" in feed["purpose"].lower()
    record = client.get(f"/surgery/cases/{case_id}/record",
                        headers=CLINICIAN).json()
    assert record["outcome"]["ssi"] is True
