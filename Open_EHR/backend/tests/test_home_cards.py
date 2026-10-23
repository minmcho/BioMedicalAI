"""Home & Module UX pass (WS1a) — GET /home/cards + effective modules.

Pins the fixed contract: {cards: {module: {value, label, status_text,
tone, timer_text?}}}; providers reuse the indicators NAMED_SOURCES
computations + continuum boarding; modules without an honest
provider are absent; the patient role gets an empty set.

Synthetic data only.
"""
from app.indicators import NAMED_SOURCES
from tests.conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

EXPECTED_MODULES = {"inbox", "lab", "pharmacy", "beds", "command",
                    "ed", "safety", "workforce", "scheduling"}
TONES = {"ok", "warn", "bad", "info", "neutral"}


def test_home_cards_requires_auth(client):
    assert client.get("/home/cards").status_code == 401


def test_home_cards_patient_gets_empty_set(client):
    r = client.get("/home/cards", headers=PATIENT)
    assert r.status_code == 200
    assert r.json() == {"cards": {}}


def test_home_cards_shape_and_providers_present(client):
    r = client.get("/home/cards", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    cards = r.json()["cards"]
    assert EXPECTED_MODULES <= set(cards), set(cards)
    for module, card in cards.items():
        assert set(card) <= {"value", "label", "status_text", "tone",
                             "timer_text"}, module
        assert isinstance(card["value"], (int, float)), module
        assert not isinstance(card["value"], bool), module
        assert isinstance(card["label"], str) and card["label"], module
        assert isinstance(card["status_text"], str) \
            and card["status_text"], module
        assert card["tone"] in TONES, module
        if "timer_text" in card:
            assert isinstance(card["timer_text"], str), module


def test_home_cards_no_provider_modules_absent(client):
    cards = client.get("/home/cards", headers=ADMIN).json()["cards"]
    for module in ("patients", "portal", "studio", "imports", "reports",
                   "radiology", "surgery", "pophealth", "teams",
                   "documents", "devices", "billing", "aftercare",
                   "unit-board", "clinic", "er-ops", "data-platform",
                   "federation", "research", "analytics", "support"):
        assert module not in cards, module


def test_home_cards_values_match_named_sources(client):
    """The cards ARE the named-source computations, not copies: with
    the same seeded database, lab/beds/safety/workforce values equal
    the NAMED_SOURCES results evaluated through the indicator API's
    own registry (checked via a fresh session on the same app)."""
    cards = client.get("/home/cards", headers=ADMIN).json()["cards"]
    factory = client.app.state.session_factory
    with factory() as session:
        assert cards["lab"]["value"] == \
            NAMED_SOURCES["lab.critical-open"](session)
        assert cards["inbox"]["value"] == \
            NAMED_SOURCES["lab.pending-verification"](session)
        assert cards["beds"]["value"] == \
            NAMED_SOURCES["census.current"](session)
        assert cards["safety"]["value"] == \
            NAMED_SOURCES["safety.open-events"](session)
        assert cards["workforce"]["value"] == \
            NAMED_SOURCES["workforce.staff-active"](session)
        assert cards["scheduling"]["value"] == \
            NAMED_SOURCES["registration.frontdesk-appointments"](session)
        assert cards["command"]["value"] == \
            NAMED_SOURCES["continuum.boarding-total"](session)


def test_home_cards_boarding_timer_text(client):
    cards = client.get("/home/cards", headers=ADMIN).json()["cards"]
    for module in ("command", "ed"):
        if cards[module]["value"] > 0:
            assert cards[module].get("timer_text", "") \
                .startswith("oldest wait"), module
        else:
            assert "timer_text" not in cards[module], module


def test_home_cards_staff_roles_all_served(client):
    for headers in (ADMIN, CLINICIAN, VIEWER):
        r = client.get("/home/cards", headers=headers)
        assert r.status_code == 200, r.text
        assert EXPECTED_MODULES <= set(r.json()["cards"])


def test_effective_modules_endpoint_profiled_clinician(client):
    """End-to-end: the seeded dev-clinician sign-in is linked to a
    staff-directory entry with job role `physician`, so the
    endpoint resolves that job role and the menu is the clinician
    role resolution intersected with the physician profile, in
    published nav order."""
    r = client.get("/home/effective-modules", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_role"] == "physician"
    assert body["modules"] == [
        "patients", "inbox", "scheduling", "ed", "radiology", "lab",
        "pharmacy", "unit-board", "clinic", "aftercare"]
    assert "studio" not in body["modules"]
    assert "portal" not in body["modules"]


def test_effective_modules_endpoint_admin_operator_exempt(client):
    """dev-admin IS staff-linked: the seeded demo directory
    (app.people _STAFF_SEED, staff-011) maps the dev-admin sign-in
    to Elena Petrova, MD with job role `executive`. That link used
    to narrow the administrator's menu to the executive profile's
    six modules — Laboratory, Pharmacy, Surgery and Studio all
    vanished from the admin's card Home (live defect on Min's Air).
    Operator roles are EXEMPT from job-profile filtering: the
    endpoint reports job_role null and the FULL admin role
    resolution (29 modules, portal excluded)."""
    r = client.get("/home/effective-modules", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_role"] is None
    for module in ("studio", "imports", "lab", "pharmacy", "surgery"):
        assert module in body["modules"], module
    assert "portal" not in body["modules"]
    assert len(body["modules"]) == 30


def test_effective_modules_endpoint_configurator_operator_exempt(client):
    """The dev-configurator sign-in is staff-linked to a
    department-head (seeded dev tokens: configurator ->
    department-head). Configurator is the second operator role:
    same exemption — job_role null, full role resolution, Studio
    present (department-head's 8-module profile must NOT apply)."""
    from tests.conftest import CONFIG
    r = client.get("/home/effective-modules", headers=CONFIG)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_role"] is None
    for module in ("studio", "lab", "pharmacy", "surgery"):
        assert module in body["modules"], module
    assert "portal" not in body["modules"]
    assert len(body["modules"]) == 29  # configurator resolution: 31 - portal - pophealth


def test_effective_modules_endpoint_unlinked_account_unprofiled(
        client, monkeypatch):
    """The job_role-is-None case needs a GENUINELY unlinked
    sign-in: every seeded dev token is staff-linked (clinician ->
    physician, viewer -> unit-manager, configurator ->
    department-head, admin -> executive). An extra dev token whose
    username has no StaffMember record and no staff-linked account
    resolves job_role None, and its menu is the raw admin role
    resolution (29 modules, portal excluded)."""
    import json as _json
    monkeypatch.setenv("OPENEHR_DEV_TOKENS", _json.dumps(
        {"unlinked-token": {"username": "dev-unlinked",
                            "role": "admin"}}))
    r = client.get("/home/effective-modules",
                   headers={"Authorization": "Bearer unlinked-token"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_role"] is None
    assert "studio" in body["modules"] and "imports" in body["modules"]
    assert "portal" not in body["modules"]
    assert len(body["modules"]) == 30


def test_effective_modules_endpoint_patient(client):
    r = client.get("/home/effective-modules", headers=PATIENT)
    assert r.status_code == 200, r.text
    assert r.json()["modules"] == ["portal", "billing"]
    assert r.json()["job_role"] is None


def test_effective_modules_endpoint_requires_auth(client):
    assert client.get("/home/effective-modules").status_code == 401
