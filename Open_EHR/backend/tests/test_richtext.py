"""Rich text (Home & Module UX pass) — app/richtext.py + wiring.

Unit: the whitelist sanitizer and the html_to_text plain rendering.
Integration: clinical notes (documentation.py) sanitize prose
answers + note text on write and render plain text into the FHIR
DocumentReference Narrative; discharge summaries ride the same
note path (note-discharge template); messages (engagement.py)
keep sanitized HTML as the app-facing body while the FHIR
Communication payload carries the plain-text rendering.
"""
import json

from app import crypto
from app.documentation import default_form_payloads
from app.richtext import (html_to_text, is_rich_html, sanitize_html,
                          strip_to_text)
from tests.conftest import CLINICIAN, PATIENT


# ---------------------------------------------------------------------------
# sanitize_html — unit
# ---------------------------------------------------------------------------

def test_allowed_formatting_survives_intact():
    src = ("<p>Hello <strong>world</strong>, <em>this</em> is "
           "<b>bold</b> <i>italic</i> <u>underlined</u></p>"
           "<h2>Head</h2><h3>Sub</h3><blockquote>quote</blockquote>"
           "<ul><li>one</li><li>two</li></ul>"
           "<ol><li>first</li></ol><p><code>x = 1</code></p>")
    assert sanitize_html(src) == src


def test_script_and_style_content_dropped_entirely():
    assert sanitize_html("<script>alert(1)</script><p>ok</p>") == "<p>ok</p>"
    assert sanitize_html("<style>.x{color:red}</style>text") == "text"
    assert sanitize_html(
        "<p>a</p><script>var s = '</p>';</script><p>b</p>") == \
        "<p>a</p><p>b</p>"


def test_other_tags_stripped_text_preserved():
    assert sanitize_html("<div>keep <span>this</span> text</div>") == \
        "keep this text"
    assert sanitize_html("<iframe src='x'>inner</iframe>") == "inner"
    assert sanitize_html("<img src=x onerror=alert(1)>after") == "after"
    assert sanitize_html("<table><tr><td>cell</td></tr></table>") == "cell"


def test_all_attributes_stripped_except_safe_href():
    assert sanitize_html("<p class='x' style='color:red' id='y'>t</p>") == \
        "<p>t</p>"
    assert sanitize_html(
        "<a href='https://example.org' onclick='evil()' target='_blank'>"
        "l</a>") == '<a href="https://example.org">l</a>'


def test_dangerous_hrefs_dropped_tag_kept():
    for href in ("javascript:alert(1)", "JaVaScRiPt:alert(1)",
                 "data:text/html,<b>x</b>", "vbscript:x",
                 "/relative/path", "//evil.example/x", "#anchor",
                 "jav&#x09;ascript:alert(1)", ""):
        out = sanitize_html(f"<a href='{href}'>click</a>")
        assert out == "<a>click</a>", href


def test_safe_href_schemes_kept():
    assert sanitize_html("<a href='http://a.org/x'>a</a>") == \
        '<a href="http://a.org/x">a</a>'
    assert sanitize_html("<a href='https://a.org/x?y=1&z=2'>a</a>") == \
        '<a href="https://a.org/x?y=1&amp;z=2">a</a>'
    assert sanitize_html("<a href='mailto:team@a.org'>a</a>") == \
        '<a href="mailto:team@a.org">a</a>'


def test_malformed_html_is_balanced():
    assert sanitize_html("<p>unclosed <strong>bold") == \
        "<p>unclosed <strong>bold</strong></p>"
    assert sanitize_html("</p>stray") == "stray"
    assert sanitize_html("<ul><li>one<li>two</ul>") == \
        "<ul><li>one</li><li>two</li></ul>"
    assert sanitize_html("<p>a<p>b") == "<p>a</p><p>b</p>"
    # Misnested inline tags close in stack order.
    assert sanitize_html("<b><i>x</b></i>") == "<b><i>x</i></b>"


def test_text_is_escaped_not_markup():
    assert sanitize_html("3 < 5 & 6 > 2") == "3 &lt; 5 &amp; 6 &gt; 2"
    assert sanitize_html("&lt;script&gt;") == "&lt;script&gt;"


def test_sanitize_is_idempotent():
    samples = [
        "<p>Hello <strong>world</strong></p>",
        "<script>alert(1)</script><p onclick='x'>a &amp; b</p>",
        "<a href='https://x.org/?a=1&b=2'>l</a><ul><li>one<li>two</ul>",
        "<p>unclosed <em>em",
        "plain & <b>bold</b> 3 < 5",
        "<a href='javascript:x'>j</a><iframe>inner</iframe>",
    ]
    for sample in samples:
        once = sanitize_html(sample)
        assert sanitize_html(once) == once, sample


def test_sanitize_non_string_inputs():
    assert sanitize_html(None) == ""
    assert sanitize_html(42) == ""
    assert sanitize_html("") == ""
    assert html_to_text(None) == ""
    assert html_to_text(3.5) == ""


# ---------------------------------------------------------------------------
# html_to_text — unit
# ---------------------------------------------------------------------------

def test_html_to_text_blocks_and_headings():
    src = "<h2>Head</h2><p>para one</p><p>para two</p>"
    assert html_to_text(src) == "Head\n\npara one\n\npara two"


def test_html_to_text_lists_and_breaks():
    assert html_to_text("<ul><li>one</li><li>two</li></ul>") == \
        "• one\n• two"
    assert html_to_text("<p>line1<br>line2</p>") == "line1\nline2"
    assert html_to_text("<blockquote>quoted</blockquote>") == "quoted"


def test_html_to_text_links():
    src = ("<p>See <a href='https://x.org'>the site</a> or "
           "<a href='https://y.org'>https://y.org</a> or "
           "<a>no link</a></p>")
    assert html_to_text(src) == \
        "See the site (https://x.org) or https://y.org or no link"


def test_html_to_text_drops_script_and_style():
    assert html_to_text("<script>x</script><style>y</style>plain") == "plain"
    assert html_to_text("no tags at all") == "no tags at all"
    assert strip_to_text("<p>a <b>b</b></p>") == "a b"


def test_is_rich_html_helper():
    assert is_rich_html("<p>x</p>") is True
    assert is_rich_html("plain text") is False
    assert is_rich_html("<div>x</div>") is False
    assert is_rich_html(None) is False


# ---------------------------------------------------------------------------
# Template seed: prose items are marked rich
# ---------------------------------------------------------------------------

def test_documentation_templates_mark_prose_items_rich():
    forms = default_form_payloads()
    for key in ("note-progress", "note-hp", "note-procedure",
                "note-discharge"):
        for item in forms[key]["item"]:
            if item["type"] == "text":
                assert item["rich"] is True, (key, item["linkId"])
            else:
                assert "rich" not in item, (key, item["linkId"])


# ---------------------------------------------------------------------------
# Notes integration
# ---------------------------------------------------------------------------

def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": ["Rich"]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _answers_of(client, response_id):
    resp = client.get(f"/fhir/QuestionnaireResponse/{response_id}",
                      headers=CLINICIAN)
    assert resp.status_code == 200, resp.text
    return {item["linkId"]: item["answer"][0].get("valueString")
            for item in resp.json()["item"]}


def test_note_answers_sanitized_on_write_fhir_rendering_plain(client):
    pid = _patient(client, "rich-1")
    r = client.post("/notes", json={
        "patient": pid, "note_type": "progress",
        "answers": [
            {"linkId": "subjective", "answer": [{"valueString":
                "<p>Feels <strong>better</strong></p>"
                "<script>alert(1)</script>"}]},
            {"linkId": "assessment", "answer": [{"valueString":
                "<p>Improving</p><img src=x onerror=alert(1)>"}]},
            {"linkId": "plan", "answer": [{"valueString":
                "<p>Continue <a href='javascript:alert(1)'>plan</a> "
                "<a href='https://example.org'>link</a></p>"}]},
        ]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    note = r.json()

    # Stored body (the QuestionnaireResponse) holds sanitized HTML.
    answers = _answers_of(client, note["noteContent"]
                          ["questionnaireResponseId"])
    assert answers["subjective"] == "<p>Feels <strong>better</strong></p>"
    assert answers["assessment"] == "<p>Improving</p>"
    assert answers["plan"] == ('<p>Continue <a>plan</a> '
                                '<a href="https://example.org">link</a></p>')

    # The FHIR rendering is plain text: the decorated plainText and
    # the DocumentReference Narrative carry no author tags at all.
    assert note["plainText"] == ("Feels better\n\nImproving\n\n"
                                  "Continue plan link (https://example.org)")
    assert "<" not in note["plainText"]
    doc = client.get(f"/fhir/DocumentReference/{note['id']}",
                     headers=CLINICIAN).json()
    assert doc["text"]["status"] == "generated"
    div = doc["text"]["div"]
    assert "Feels better" in div and "alert" not in div
    assert "<strong>" not in div and "<script" not in div
    # The stored extension still carries the sanitized HTML content.
    ext = [e for e in doc["extension"]
           if e["url"].endswith("note-content")][0]
    stored = json.loads(ext["valueString"])
    assert stored["questionnaireResponseId"] == \
        note["noteContent"]["questionnaireResponseId"]


def test_note_free_text_sanitized_and_plain_rendering(client):
    pid = _patient(client, "rich-2")
    r = client.post("/notes", json={
        "patient": pid, "note_type": "progress",
        "text": "<p>Seen <em>at bedside</em></p><script>x</script>",
        "sections": [{"title": "Plan",
                      "body": "<p>Review <b>tomorrow</b></p>"}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    note = r.json()
    assert note["noteContent"]["text"] == \
        "<p>Seen <em>at bedside</em></p>"
    assert note["noteContent"]["sections"] == [
        {"title": "Plan", "body": "<p>Review <b>tomorrow</b></p>"}]
    # Section strings render in document order (title, then body),
    # then the note's free text.
    assert note["plainText"] == "Plan\n\nReview tomorrow\n\nSeen at bedside"
    assert "<" not in note["plainText"]


def test_discharge_summary_sanitized_via_note_path(client):
    pid = _patient(client, "rich-3")
    r = client.post("/notes", json={
        "patient": pid, "note_type": "discharge",
        "answers": [
            {"linkId": "hospital-course", "answer": [{"valueString":
                "<p>Course <b>stable</b></p><script>bad</script>"}]},
            {"linkId": "follow-up", "answer": [{"valueString":
                "<p>Return in <u>2 weeks</u></p>"}]},
        ]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    note = r.json()
    answers = _answers_of(client, note["noteContent"]
                          ["questionnaireResponseId"])
    assert answers["hospital-course"] == "<p>Course <b>stable</b></p>"
    assert answers["follow-up"] == "<p>Return in <u>2 weeks</u></p>"
    assert note["plainText"] == "Course stable\n\nReturn in 2 weeks"


def test_flowsheet_prose_answer_sanitized_via_shared_store(client):
    # Flowsheets (and continuum's discharge forms) share
    # documentation._store_response with notes: their text answers
    # are sanitized by the same write path.
    pid = _patient(client, "rich-4")
    r = client.post("/flowsheets", json={
        "patient": pid, "form": "flowsheet-assessment",
        "answers": [
            {"linkId": "consciousness",
             "answer": [{"valueCoding": {"code": "alert"}}]},
            {"linkId": "notes", "answer": [{"valueString":
                "<p>Resting</p><script>bad</script>"}]},
        ]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    answers = _answers_of(client, r.json()["id"])
    assert answers["notes"] == "<p>Resting</p>"
    view = client.get(f"/flowsheets/flowsheet-assessment?patient={pid}",
                      headers=CLINICIAN).json()
    items = {i["linkId"]: i for i in view["form"]["items"]}
    assert items["notes"]["rich"] is True
    assert items["consciousness"]["rich"] is False


# ---------------------------------------------------------------------------
# Messages integration (engagement.py)
# ---------------------------------------------------------------------------

def _stored_communication(client, message_id):
    r = client.get(f"/fhir/Communication/{message_id}", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _decrypt(value):
    return crypto.decrypt(json.loads(value))


def test_rich_message_app_html_fhir_payload_plain(client):
    _patient(client, "pat-1")
    r = client.post("/messages", json={
        "patient": "pat-1",
        "text": ("<p>Your result is <strong>ready</strong>.</p>"
                 "<script>alert(1)</script>"
                 "<p>See <a href='https://example.org'>details</a></p>")},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    message = r.json()
    expected_html = ('<p>Your result is <strong>ready</strong>.</p>'
                     '<p>See <a href="https://example.org">details</a></p>')
    # App-facing body: sanitized HTML.
    assert message["text"] == expected_html
    detail = client.get(f"/messages/threads/{message['threadId']}",
                        headers=CLINICIAN).json()
    assert detail["messages"][0]["text"] == expected_html

    # FHIR Communication payload: plain text only (decrypted from
    # its envelope — the stored row itself holds no plaintext).
    stored = _stored_communication(client, message["id"])
    payload_text = _decrypt(stored["payload"][0]["contentString"])
    assert payload_text == ("Your result is ready.\n\n"
                            "See details (https://example.org)")
    assert "<" not in payload_text
    rich_ext = [e for e in stored["extension"]
                if e["url"].endswith("message-rich-html")]
    assert len(rich_ext) == 1
    assert _decrypt(rich_ext[0]["valueString"]) == expected_html


def test_plain_message_storage_unchanged(client):
    _patient(client, "pat-1")
    r = client.post("/portal/messages",
                    json={"text": "Question about my lab"},
                    headers=PATIENT)
    assert r.status_code == 201, r.text
    message = r.json()
    assert message["text"] == "Question about my lab"
    stored = _stored_communication(client, message["id"])
    assert _decrypt(stored["payload"][0]["contentString"]) == \
        "Question about my lab"
    assert not [e for e in stored["extension"]
                if e["url"].endswith("message-rich-html")]


def test_hostile_markup_only_message_reduced_to_text(client):
    _patient(client, "pat-1")
    r = client.post("/messages", json={
        "patient": "pat-1",
        "text": "<script>alert(1)</script><div>hello</div>"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["text"] == "hello"
    stored = _stored_communication(client, r.json()["id"])
    assert _decrypt(stored["payload"][0]["contentString"]) == "hello"
