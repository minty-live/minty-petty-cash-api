"""The Expenses step (``petty_cash/routes/expenses.py``) - Flask's
``tests/test_petty_cash_api_expenses.py`` expectations, restated. The bucket is an in-memory fake at
the S3 client (``petty_cash/files/storage.client``)."""

from __future__ import annotations

import os
import struct
import uuid
import zlib
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from minty_db.enums import EntityRole
from minty_db.models import (
    AccountInfo,
    AiPolicy,
    Attachment,
    EntityAccountXero,
    Report,
    ReportExpense,
    ReportExpenseAttachment,
    ReportHistory,
    UserEntity,
    XeroContactSync,
)

from petty_cash.days import today
from petty_cash.files import storage
from petty_cash.tests.conftest import WEB, headers, make_entity, make_report, make_user, url

pytestmark = pytest.mark.django_db
D3 = today() - timedelta(days=1)
PNG = b"\x89PNG\r\n\x1a\n" + b"small-png-bytes"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


class FakeS3:
    """The S3 client the storage module calls, in memory."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def upload_fileobj(self, fileobj, bucket, key, **kwargs):
        self.objects[key] = fileobj.read()
        self.extra_args = kwargs.get("ExtraArgs")

    def get_object(self, *, Bucket, Key, **kwargs):
        return {"Body": BytesIO(self.objects[Key])}

    def delete_objects(self, *, Bucket, Delete, **kwargs):
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)
            self.deleted.append(obj["Key"])


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage, "client", lambda: fake)
    return fake


@pytest.fixture
def lists(shop):
    """Two synced suppliers; one account on for Petty Cash, one switched off."""
    XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id="con-0002", name="Taxi Co")
    XeroContactSync.objects.create(entity_id=shop.entity.id, xero_contact_id="con-0001", name="ABC Supplies")
    on = AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id="acc-0001", xero_code="400",
                                    name="Advertising", type="EXPENSE", status="ACTIVE")
    off = AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id="acc-0002", xero_code="410",
                                     name="Cleaning", type="EXPENSE", status="ACTIVE")
    EntityAccountXero.objects.create(account_id=on.id, name="Advertising", type="EXPENSE", is_active=True)
    EntityAccountXero.objects.create(account_id=off.id, name="Cleaning", type="EXPENSE", is_active=False)


def _draft(shop, **fields):
    values = {"opening_balance": 100, "cash_addition": 20, "adjusted_opening_balance": 120, "cashsale_total": 50,
              "closing_balance": 170, "bank_deposit": 5, "completed_sections": ["opening", "sales"],
              "current_section": "expenses"}
    values.update(fields)
    return make_report(shop.entity, D3, status=values.pop("status", "draft"), created_by=shop.owner.id, **values)


def _get(client, shop, user=None, day=D3):
    query = {"transaction_date": day.isoformat()} if day else {}
    return client.get(url(shop.entity.id, "/reports/expenses"), query, headers=headers((user or shop.owner).id))


def _add(client, shop, user=None, **over):
    form = {"transaction_date": D3.isoformat(), "amount": "1,012.50", "description": "Flyers",
            "supplier_id": "con-0001", "account_id": "acc-0001", "receipt": PNG}
    form.update(over)
    receipt = form.pop("receipt")
    if receipt is not None:
        form["receipt"] = SimpleUploadedFile("till-slip.jpg", receipt, content_type="image/jpeg")
    return client.post(url(shop.entity.id, "/reports/expenses/lines"), form, headers=headers((user or shop.owner).id))


def _line(shop, draft, s3, amount="1012.50", *, receipt=JPEG, name="till-slip.jpg"):
    """A stored line as an earlier add left it."""
    line = ReportExpense.objects.create(
        report_id=draft.id, item="Advertising", amount=Decimal(amount), description="Flyers",
        account_id=AccountInfo.objects.get(xero_account_id="acc-0001").id,
        contact_id=XeroContactSync.objects.get(xero_contact_id="con-0001").id,
    )
    key = f"expenses/{draft.id}/{line.id}.jpg"
    s3.objects[key] = receipt
    attachment = Attachment.objects.create(original_name=name, stored_name=f"{line.id}.jpg", file_path=key,
                                           file_extension="jpg", mime_type="image/jpeg")
    ReportExpenseAttachment.objects.create(report_expense_id=line.id, attachment_id=attachment.id, sort_order=0)
    return line, attachment


# --- GET ---------------------------------------------------------------------------------------------


def test_the_page_brings_the_lines_and_what_a_line_may_use(client, shop, lists, s3):
    draft = _draft(shop)
    line, _ = _line(shop, draft, s3)

    resp = _get(client, shop)

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["transaction_date"] == D3.isoformat() and body["currency_code"] == "HKD"
    assert body["draft"]["id"] == str(draft.id) and body["draft"]["completed_sections"] == ["opening", "sales"]
    assert body["suppliers"] == [{"id": "con-0001", "name": "ABC Supplies"}, {"id": "con-0002", "name": "Taxi Co"}]
    assert body["accounts"] == [{"id": "acc-0001", "code": "400", "name": "Advertising"}]
    assert body["total"] == 1012.5
    [only] = body["lines"]
    assert only["id"] == str(line.id) and only["item"] == "Advertising" and only["amount"] == 1012.5
    assert only["description"] == "Flyers"
    assert only["supplier"] == {"id": "con-0001", "name": "ABC Supplies"}
    assert only["account"] == {"id": "acc-0001", "code": "400", "name": "Advertising"}
    assert [(r["name"], r["mime_type"]) for r in only["receipts"]] == [("till-slip.jpg", "image/jpeg")]
    assert body["permissions"] == {"create_supplier": True}


def test_a_line_shows_its_account_and_supplier_as_the_chart_has_them_now(client, shop, lists, s3):
    """The user's rule (2026-11-24): a renamed account and supplier show their names now; a line on an
    account merged away shows the kept one; the saved ``item`` only for a line with no account."""
    from petty_cash.routes.expenses import lines_payload
    from petty_cash.services.report_page import _expense_lines

    draft = _draft(shop)
    line, _ = _line(shop, draft, s3)
    kept = AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id="acc-0009", xero_code="401",
                                      name="Marketing", type="EXPENSE", status="ACTIVE")
    AccountInfo.objects.filter(xero_account_id="acc-0001").update(merged_into_id=kept.id, record_status="archived")
    XeroContactSync.objects.filter(xero_contact_id="con-0001").update(name="ABC Supplies Ltd")
    bare = ReportExpense.objects.create(report_id=draft.id, item="Old words", amount=Decimal("1"), description="")

    payload = {p["id"]: p for p in lines_payload(ReportExpense.objects.filter(report_id=draft.id))}
    assert payload[str(line.id)]["item"] == "Marketing"
    assert payload[str(line.id)]["account"] == {"id": "acc-0009", "code": "401", "name": "Marketing"}
    assert payload[str(line.id)]["supplier"] == {"id": "con-0001", "name": "ABC Supplies Ltd"}
    assert (payload[str(bare.id)]["item"], payload[str(bare.id)]["account"]) == ("Old words", None)

    close = {e["id"]: e for e in _expense_lines(draft.id)}
    assert (close[str(line.id)]["item"], close[str(line.id)]["contact_name"]) == ("Marketing", "ABC Supplies Ltd")


@pytest.mark.parametrize(("switch", "enabled"), [(True, True), (False, False), (None, False)])
def test_the_page_says_whether_the_receipt_reader_is_on(client, shop, lists, ai_policy, switch, enabled):
    """The app offers "Read with AI" only when ``ai_policy.receipt_reader_on`` is on (owner, 2026-11-20;
    off by the column's default), with the company's reads left (shared with Payment Request); a
    switched-off reader sends no count. The invoice reader's switch is not this page's."""
    _draft(shop)
    if switch is None:
        AiPolicy.objects.filter(id=1).delete()
        AiPolicy.objects.create(id=1, primary_model="gemini-3.8-flash", default_grant={"tokens": 10000},
                                limit_rules=[], invoice_reader_on=True)
    else:
        AiPolicy.objects.filter(id=1).update(receipt_reader_on=switch)

    resp = _get(client, shop)

    assert resp.status_code == 200, resp.content
    expected = {"enabled": False}
    if enabled:
        expected = {"enabled": True, "reads_used": None, "reads_limit": None, "reads_left": None,
                    "percent_left": 100, "blocked": False, "resumes_at": None}
    assert resp.json()["reader"] == expected


def test_with_no_draft_the_page_goes_back_to_opening(client, shop, lists):
    resp = _get(client, shop)

    assert resp.status_code == 409
    assert resp.json() == {"error": "I don't see a draft yet - let's start with the opening entry.", "to": "opening"}


def test_a_missing_date_and_a_member_without_the_right_are_refused(client, shop, lists, s3):
    _draft(shop)
    assert _get(client, shop, day=None).status_code == 422
    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=shop.entity.id, role=EntityRole.ENTITY_BASE, approved=True)

    assert _get(client, shop, user=base).status_code == 403
    assert _add(client, shop, user=base).status_code == 403
    assert s3.objects == {}


# --- adding a line -----------------------------------------------------------------------------------


def test_a_line_is_stored_with_its_receipt_as_its_bytes_say(client, shop, lists, s3):
    draft = _draft(shop)

    resp = _add(client, shop)  # a PNG named .jpg and sent as image/jpeg

    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["total"] == 1012.5
    assert body["line"]["receipts"][0]["mime_type"] == "image/png"
    assert body["line"]["receipts"][0]["name"] == "till-slip.jpg"
    row = Report.objects.get(id=draft.id)
    assert row.expense_total == Decimal("1012.50")
    assert row.closing_balance == Decimal("100") + 20 + 50 - Decimal("1012.50") - 5
    line = ReportExpense.objects.get(report_id=draft.id)
    assert line.item == "Advertising" and line.description == "Flyers"
    assert line.contact_id == XeroContactSync.objects.get(xero_contact_id="con-0001").id
    assert line.account_id == AccountInfo.objects.get(xero_account_id="acc-0001").id
    [key] = s3.objects
    assert key.startswith(f"expenses/{draft.id}/") and key.endswith("_ADVERTISING_1012.jpg")
    history = list(ReportHistory.objects.filter(report_id=draft.id).values_list("action", "new_value"))
    assert history == [("added", "Added expense: Advertising (HKD 1,012.50)")]
    # 2026-11-03: the row said nothing of its size; it is the stored file's
    assert Attachment.objects.get(file_path=key).file_size == len(s3.objects[key])


def _big_png(side=800) -> bytes:
    """A real PNG of noise, over 1 MB: the size that used to be shrunk and re-encoded as JPEG."""
    raw = b"".join(b"\x00" + os.urandom(side * 3) for _ in range(side))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def test_a_large_receipt_is_stored_exactly_as_uploaded(client, shop, lists, s3):
    """2026-11-04 (owner): no downsizing and no conversion - B2 holds the person's file byte for byte."""
    _draft(shop)
    png = _big_png()
    assert len(png) > 1024 * 1024
    form = {"transaction_date": D3.isoformat(), "amount": "12.40", "description": "Flyers",
            "supplier_id": "con-0001", "account_id": "acc-0001",
            "receipt": SimpleUploadedFile("big-slip.png", png, content_type="image/png")}

    resp = client.post(url(shop.entity.id, "/reports/expenses/lines"), form, headers=headers(shop.owner.id))

    assert resp.status_code == 201, resp.content
    [key] = s3.objects
    assert s3.objects[key] == png and key.endswith(".png")
    assert s3.extra_args == {"ContentType": "image/png"}
    row = Attachment.objects.get(file_path=key)
    assert row.mime_type == "image/png" and row.file_size == len(png)
    assert resp.json()["line"]["receipts"][0]["mime_type"] == "image/png"


def test_two_lines_alike_keep_their_own_receipts(client, shop, lists, s3):
    _draft(shop)

    _add(client, shop, amount="12.40")
    _add(client, shop, amount="12.10")

    assert len(s3.objects) == 2  # the second did not overwrite the first


@pytest.mark.parametrize("over, message", [
    ({"amount": "0"}, "Enter the amount as a number above 0"),
    ({"amount": "-5"}, "Enter the amount as a number above 0"),
    ({"amount": "twelve"}, "Enter the amount as a number above 0"),
    ({"amount": "100000000000"}, "up to 99,999,999,999.99"),
    ({"description": "x" * 301}, "Keep the description to 300 characters or fewer."),
    ({"supplier_id": "con-unknown"}, "Choose a supplier from the list."),
    ({"supplier_id": ""}, "Choose a supplier from the list."),
    ({"account_id": "acc-0002"}, "Choose an account code from the list."),  # switched off
    ({"receipt": None}, "Attach the receipt to add this expense."),
    ({"receipt": b"GIF89a-not-allowed"}, "Attach a PDF, JPEG or PNG file."),
    ({"receipt": b""}, "That file is empty. Attach the receipt again."),
    ({"receipt": b"%PDF" + b"0" * (10 * 1024 * 1024)}, "Attach a file of 10 MB or less."),
    ({"transaction_date": "14/10/2026"}, "That date doesn't look quite right!"),
])
def test_bad_input_is_a_422_and_nothing_is_stored(client, shop, lists, s3, over, message):
    draft = _draft(shop)

    resp = _add(client, shop, **over)

    assert resp.status_code == 422
    assert message in resp.json()["error"]
    assert not ReportExpense.objects.filter(report_id=draft.id).exists()
    assert s3.objects == {}


def test_a_line_without_a_draft_goes_back_to_opening(client, shop, lists, s3):
    resp = _add(client, shop)

    assert resp.status_code == 409 and resp.json()["to"] == "opening"
    assert s3.objects == {}


def test_a_line_on_a_submitted_day_is_refused(client, shop, lists, s3):
    _draft(shop, status="submitted")

    resp = _add(client, shop)

    assert resp.status_code == 409  # no draft for the day
    assert s3.objects == {}


def test_a_failed_save_leaves_no_receipt_behind(client, shop, lists, s3, monkeypatch):
    from petty_cash.services import expenses as service

    _draft(shop)

    def broken(*args, **kwargs):
        raise RuntimeError("the row did not save")

    monkeypatch.setattr(service, "recompute_totals", broken)

    resp = _add(client, shop)

    assert resp.status_code == 500
    assert resp.json() == {"error": "Something went wrong on our side. Please try again."}
    assert s3.objects == {} and len(s3.deleted) == 1
    assert not ReportExpense.objects.exists()


# --- deleting a line ---------------------------------------------------------------------------------


def test_deleting_a_line_answers_the_new_total(client, shop, lists, s3, django_capture_on_commit_callbacks):
    draft = _draft(shop)
    _line(shop, draft, s3, "15.00")
    doomed, attachment = _line(shop, draft, s3, "40.00")
    Report.objects.filter(id=draft.id).update(expense_total=55)

    with django_capture_on_commit_callbacks(execute=True):  # the files go only once the rows are committed
        resp = client.delete(url(shop.entity.id, f"/expense-lines/{doomed.id}"), headers=headers(shop.owner.id))

    assert resp.status_code == 200 and resp.json() == {"total": 15.0}
    assert Report.objects.get(id=draft.id).expense_total == 15
    assert not ReportExpense.objects.filter(id=doomed.id).exists()
    assert not Attachment.objects.filter(id=attachment.id).exists()
    assert s3.deleted == [attachment.file_path]
    assert ReportHistory.objects.filter(report_id=draft.id, action="deleted").count() == 1


def test_a_line_of_another_company_or_a_submitted_report_is_left_alone(client, shop, lists, s3):
    other_owner = make_user("other@test.com")
    other = make_entity(other_owner, name="Other Co")
    theirs_report = make_report(other, D3, status="draft")
    theirs = ReportExpense.objects.create(report_id=theirs_report.id, item="X", amount=1)

    assert client.delete(url(shop.entity.id, f"/expense-lines/{theirs.id}"),
                         headers=headers(shop.owner.id)).status_code == 404
    assert client.delete(url(shop.entity.id, "/expense-lines/not-a-uuid"),
                         headers=headers(shop.owner.id)).status_code == 404

    submitted = _draft(shop, status="submitted")
    line, _ = _line(shop, submitted, s3)
    resp = client.delete(url(shop.entity.id, f"/expense-lines/{line.id}"), headers=headers(shop.owner.id))

    assert resp.status_code == 409
    assert resp.json()["error"] == "This report isn't a draft anymore, so I can't change its expenses."
    assert ReportExpense.objects.filter(id=line.id).exists() and s3.deleted == []


# --- a receipt ---------------------------------------------------------------------------------------


def test_a_receipt_comes_back_by_its_id_with_the_type_its_bytes_say(client, shop, lists, s3):
    draft = _draft(shop)
    line, attachment = _line(shop, draft, s3, receipt=PNG)  # stored as image/jpeg, the bytes are a PNG

    resp = client.get(url(shop.entity.id, f"/expense-lines/{line.id}/receipts/{attachment.id}"),
                      headers=headers(shop.owner.id))

    assert resp.status_code == 200
    assert resp.content == PNG and resp["Content-Type"] == "image/png"
    assert resp["X-Content-Type-Options"] == "nosniff" and resp["Content-Disposition"] == "inline"
    assert resp["Access-Control-Allow-Origin"] == WEB
    wrong = client.get(url(shop.entity.id, f"/expense-lines/{line.id}/receipts/{line.id}"),
                       headers=headers(shop.owner.id))
    assert wrong.status_code == 404 and wrong.json() == {"error": "I couldn't find that file."}


# --- the step's save ---------------------------------------------------------------------------------


def test_save_next_marks_expenses_done_and_save_exit_records_nothing(client, shop, lists):
    draft = _draft(shop)

    def put(action):
        return client.put(url(shop.entity.id, "/reports/expenses"),
                          {"transaction_date": D3.isoformat(), "action": action},
                          content_type="application/json", headers=headers(shop.owner.id))

    exit_ = put("exit")
    assert exit_.status_code == 200 and exit_.json()["next"] == "dashboard"
    assert Report.objects.get(id=draft.id).completed_sections == ["opening", "sales"]

    nxt = put("next")
    assert nxt.status_code == 200
    assert nxt.json()["next"] == "deposit" and nxt.json()["current_section"] == "deposit"
    assert Report.objects.get(id=draft.id).completed_sections == ["opening", "sales", "expenses"]

    assert put("later").status_code == 422


# --- editing a draft's line (PATCH /expense-lines/{id}) ----------------------------------------------
# Flask's draft-edit section of tests/test_petty_cash_api_expenses.py, restated.


def _edit(client, shop, line_id, user=None, *, receipt=None, name="new.png", **form):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart

    data = dict(form)
    if receipt is not None:
        data["receipt"] = SimpleUploadedFile(name, receipt, content_type="image/png")
    return client.patch(url(shop.entity.id, f"/expense-lines/{line_id}"), encode_multipart(BOUNDARY, data),
                        content_type=MULTIPART_CONTENT, headers=headers((user or shop.owner).id))


def _second(draft, line, s3, key="expenses/second.jpg"):
    s3.objects[key] = JPEG
    attachment = Attachment.objects.create(original_name="second.jpg", stored_name="second.jpg", file_path=key,
                                           file_extension="jpg", mime_type="image/jpeg")
    ReportExpenseAttachment.objects.create(report_expense_id=line.id, attachment_id=attachment.id, sort_order=1)
    return attachment


def test_a_draft_line_is_changed_in_place_and_the_totals_follow(client, shop, lists, s3):
    printing = AccountInfo.objects.create(entity_id=shop.entity.id, xero_account_id="acc-0003", xero_code="461",
                                          name="Printing", type="EXPENSE", status="ACTIVE")
    EntityAccountXero.objects.create(account_id=printing.id, name="Printing", type="EXPENSE", is_active=True)
    draft = _draft(shop)
    line, attachment = _line(shop, draft, s3, "10.00")
    _line(shop, draft, s3, "15.00")

    resp = _edit(client, shop, line.id, amount="1,234.50", description="  Posters ", supplier_id="con-0002",
                 account_id="acc-0003")

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["total"] == 1249.5
    assert body["line"] == {
        "id": str(line.id), "item": "Printing", "amount": 1234.5, "description": "Posters",
        "supplier": {"id": "con-0002", "name": "Taxi Co"},
        "account": {"id": "acc-0003", "code": "461", "name": "Printing"},
        "receipts": [{"id": str(attachment.id), "name": "till-slip.jpg", "mime_type": "image/jpeg"}],
    }
    row = Report.objects.get(id=draft.id)
    assert row.expense_total == Decimal("1249.50")
    assert row.closing_balance == Decimal("100") + 20 + 50 - Decimal("1249.50") - 5
    history = list(ReportHistory.objects.filter(report_id=draft.id).values_list("action", "new_value"))
    assert history == [("updated", "updated to amount: HKD 1,234.50")]
    assert resp["Access-Control-Allow-Origin"] == WEB


def test_fields_not_sent_stay_and_an_empty_description_clears(client, shop, lists, s3):
    draft = _draft(shop)
    line, _ = _line(shop, draft, s3, "10.00")

    resp = _edit(client, shop, line.id, description="")

    assert resp.status_code == 200
    changed = resp.json()["line"]
    assert changed["description"] == "" and changed["amount"] == 10.0 and changed["item"] == "Advertising"
    assert changed["supplier"] == {"id": "con-0001", "name": "ABC Supplies"}


def test_a_new_receipt_replaces_the_line_s_and_the_old_file_goes(client, shop, lists, s3,
                                                                  django_capture_on_commit_callbacks):
    draft = _draft(shop)
    line, attachment = _line(shop, draft, s3, "10.00")

    with django_capture_on_commit_callbacks(execute=True):
        resp = _edit(client, shop, line.id, receipt=PNG, name="scan.pdf")

    assert resp.status_code == 200, resp.content
    [receipt] = resp.json()["line"]["receipts"]
    assert receipt["name"] == "scan.pdf" and receipt["mime_type"] == "image/png"  # typed by its bytes
    assert receipt["id"] != str(attachment.id)
    assert s3.deleted == [attachment.file_path] and not Attachment.objects.filter(id=attachment.id).exists()
    [key] = s3.objects
    assert s3.objects[key] == PNG and key.endswith("_ADVERTISING_10.pdf")  # named as adding names it


def test_a_receipt_can_be_removed_but_never_the_last_one(client, shop, lists, s3,
                                                         django_capture_on_commit_callbacks):
    draft = _draft(shop)
    line, first = _line(shop, draft, s3, "10.00")
    second = _second(draft, line, s3)

    with django_capture_on_commit_callbacks(execute=True):
        resp = _edit(client, shop, line.id, remove_receipt_ids=second.id)

    assert resp.status_code == 200, resp.content
    assert [r["id"] for r in resp.json()["line"]["receipts"]] == [str(first.id)]
    assert s3.deleted == ["expenses/second.jpg"]

    last = _edit(client, shop, line.id, remove_receipt_ids=first.id)
    assert last.status_code == 422
    assert last.json()["error"] == (
        "Every expense needs its receipt. Attach a new receipt before removing the last one.")
    stranger = _edit(client, shop, line.id, remove_receipt_ids=str(uuid.uuid4()))
    assert stranger.status_code == 422 and "isn't on this expense" in stranger.json()["error"]

    swap = _edit(client, shop, line.id, remove_receipt_ids=first.id, receipt=PNG)
    assert swap.status_code == 200
    assert [r["mime_type"] for r in swap.json()["line"]["receipts"]] == ["image/png"]


@pytest.mark.parametrize("over, message", [
    ({"amount": "0"}, "Enter the amount as a number above 0"),
    ({"amount": "-5"}, "Enter the amount as a number above 0"),
    ({"amount": ""}, "Enter the amount as a number above 0"),
    ({"amount": "twelve"}, "Enter the amount as a number above 0"),
    ({"amount": "100000000000"}, "up to 99,999,999,999.99"),
    ({"description": "x" * 301}, "Keep the description to 300 characters or fewer."),
    ({"supplier_id": "con-unknown"}, "Choose a supplier from the list."),
    ({"account_id": "acc-0002"}, "Choose an account code from the list."),  # switched off
    ({"receipt": b"GIF89a-not-allowed"}, "Attach a PDF, JPEG or PNG file."),
    ({"receipt": b""}, "That file is empty. Attach the receipt again."),
    ({"receipt": b"%PDF" + b"0" * (10 * 1024 * 1024)}, "Attach a file of 10 MB or less."),
], ids=["zero", "negative", "empty", "words", "too-much", "description", "supplier", "account", "gif", "no-bytes",
        "too-big"])
def test_bad_input_for_an_edit_is_a_422_and_nothing_changes(client, shop, lists, s3, over, message):
    draft = _draft(shop)
    line, _ = _line(shop, draft, s3, "10.00")
    before = _get(client, shop).json()["lines"]
    stored = dict(s3.objects)

    resp = _edit(client, shop, line.id, **over)

    assert resp.status_code == 422
    assert message in resp.json()["error"]
    assert _get(client, shop).json()["lines"] == before
    assert s3.objects == stored and s3.deleted == []


def test_an_edit_is_refused_for_another_company_a_submitted_report_or_no_right(client, shop, lists, s3):
    other_owner = make_user("other@test.com")
    other = make_entity(other_owner, name="Other Co")
    theirs = ReportExpense.objects.create(report_id=make_report(other, D3, status="draft").id, item="X", amount=1)
    assert _edit(client, shop, theirs.id, amount="99").status_code == 404
    assert _edit(client, shop, "not-a-uuid", amount="99").status_code == 404
    assert _edit(client, shop, uuid.uuid4(), amount="99").status_code == 404

    draft = _draft(shop)
    line, _ = _line(shop, draft, s3, "10.00")
    base = make_user("base@test.com")
    UserEntity.objects.create(user_id=base.id, entity_id=shop.entity.id, role=EntityRole.ENTITY_BASE, approved=True)
    refused = _edit(client, shop, line.id, user=base, amount="99")
    assert refused.status_code == 403
    assert refused.json()["error"] == "You do not have permission to edit reports for this entity."

    Report.objects.filter(id=draft.id).update(status="submitted")
    resp = _edit(client, shop, line.id, amount="99")
    assert resp.status_code == 409
    assert resp.json()["error"] == "This report isn't a draft anymore, so I can't change its expenses."
    assert ReportExpense.objects.get(id=line.id).amount == Decimal("10.00")


def test_a_failed_edit_leaves_the_new_receipt_out_and_the_old_one_kept(client, shop, lists, s3, monkeypatch):
    from petty_cash.services import expenses as service

    draft = _draft(shop)
    line, attachment = _line(shop, draft, s3, "10.00")

    def broken(*args, **kwargs):
        raise RuntimeError("the row did not save")

    monkeypatch.setattr(service, "recompute_totals", broken)

    resp = _edit(client, shop, line.id, receipt=PNG, amount="99")

    assert resp.status_code == 500
    assert list(s3.objects) == [attachment.file_path] and len(s3.deleted) == 1
    assert ReportExpense.objects.get(id=line.id).amount == Decimal("10.00")
    assert ReportExpenseAttachment.objects.get(report_expense_id=line.id).attachment_id == attachment.id


# --- a contact created while adding a line -------------------------------------------------------


def test_a_contact_created_on_the_expenses_step_is_saved_and_carried_by_the_new_line(
        client, shop, lists, s3, monkeypatch):
    """The web app's flow: POST /suppliers, then add the line with the id it answered. The new
    contact is a xero_contact_sync row, the line points at it, and the step lists it after."""
    from minty_db.models import Entity

    from petty_cash.tests.test_suppliers import ORG, FakeHttp

    fake = FakeHttp()
    monkeypatch.setattr("requests.post", fake.post)
    monkeypatch.setattr("requests.get", fake.get)  # Xero is searched for the name first (C5)
    Entity.objects.filter(id=shop.entity.id).update(xero_org_id=ORG, status="connected")
    draft = _draft(shop)

    made = client.post(url(shop.entity.id, "/suppliers"), {"name": "Corner Bakery"},
                       content_type="application/json", headers=headers(shop.owner.id))
    assert made.status_code == 201, made.content
    assert made.json() == {"id": "con-new", "name": "Corner Bakery"}
    saved = XeroContactSync.objects.get(entity_id=shop.entity.id, xero_contact_id="con-new")
    assert (saved.name, saved.xero_org_id) == ("Corner Bakery", ORG)

    added = _add(client, shop, supplier_id=made.json()["id"])
    assert added.status_code in (200, 201), added.content

    [line] = ReportExpense.objects.filter(report_id=draft.id)
    assert line.contact_id == saved.id
    page = _get(client, shop).json()
    assert {"id": "con-new", "name": "Corner Bakery"} in page["suppliers"]
    assert page["lines"][0]["supplier"] == {"id": "con-new", "name": "Corner Bakery"}


# ---- a supplier the receipt reader read (the user, 2026-11-23) ---------------------------------------


def _token(shop, name="Corner Bakery", entity_id=None):
    from django.conf import settings
    from minty_shared import ai

    return ai.new_supplier_token(settings.SECRET_KEY, entity_id or shop.entity.id, name)


def test_a_read_supplier_the_company_lacks_is_added_with_the_line(client, shop, lists, s3):
    """Offline (no Xero organisation): the supplier is the company's own row, and the line uses it."""
    draft = _draft(shop)

    added = _add(client, shop, supplier_id="", new_supplier_token=_token(shop))

    assert added.status_code == 201, added.content
    made = XeroContactSync.objects.get(entity_id=shop.entity.id, name="Corner Bakery")
    assert (made.xero_contact_id, made.source, made.record_status) == (None, "user", "active")
    [line] = ReportExpense.objects.filter(report_id=draft.id)
    assert line.contact_id == made.id
    assert added.json()["line"]["supplier"] == {"id": str(made.id), "name": "Corner Bakery"}


def test_a_read_supplier_is_made_in_xero_when_live(client, shop, lists, s3, monkeypatch):
    from minty_db.models import Entity

    from petty_cash.tests.test_suppliers import ORG, FakeHttp

    fake = FakeHttp()
    monkeypatch.setattr("requests.post", fake.post)
    monkeypatch.setattr("requests.get", fake.get)
    Entity.objects.filter(id=shop.entity.id).update(xero_org_id=ORG, status="connected")
    draft = _draft(shop)

    added = _add(client, shop, supplier_id="", new_supplier_token=_token(shop))

    assert added.status_code == 201, added.content
    made = XeroContactSync.objects.get(entity_id=shop.entity.id, xero_contact_id="con-new")
    assert ReportExpense.objects.get(report_id=draft.id).contact_id == made.id


def test_a_read_supplier_the_company_has_is_that_one(client, shop, lists, s3):
    draft = _draft(shop)
    before = XeroContactSync.objects.count()

    added = _add(client, shop, supplier_id="", new_supplier_token=_token(shop, "abc  supplies"))

    assert added.status_code == 201, added.content
    assert XeroContactSync.objects.count() == before
    held = XeroContactSync.objects.get(xero_contact_id="con-0001")
    assert ReportExpense.objects.get(report_id=draft.id).contact_id == held.id


def test_a_cashier_adds_a_read_supplier_too(client, shop, lists, s3):
    _draft(shop)
    cashier = make_user("cashier@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=shop.entity.id, role=EntityRole.CASHIER, approved=True)

    added = _add(client, shop, user=cashier, supplier_id="", new_supplier_token=_token(shop))

    assert added.status_code == 201, added.content
    assert XeroContactSync.objects.filter(entity_id=shop.entity.id, name="Corner Bakery").exists()


@pytest.mark.parametrize("token", ["forged.token", "other"], ids=["altered", "another-company"])
def test_a_bad_reader_token_is_a_422_and_nothing_is_added(client, shop, lists, s3, token):
    _draft(shop)
    if token == "other":
        token = _token(shop, entity_id=uuid.uuid4())

    added = _add(client, shop, supplier_id="", new_supplier_token=token)

    assert added.status_code == 422
    assert added.json()["error"] == "The suggested contact has expired. Choose a contact from the list or add it."
    assert not XeroContactSync.objects.filter(name="Corner Bakery").exists()
    assert not ReportExpense.objects.exists() and s3.objects == {}


def test_a_line_refused_for_another_reason_adds_no_supplier(client, shop, lists, s3):
    _draft(shop)

    added = _add(client, shop, supplier_id="", new_supplier_token=_token(shop), amount="0")

    assert added.status_code == 422
    assert not XeroContactSync.objects.filter(name="Corner Bakery").exists()


def test_a_chosen_supplier_wins_over_a_reader_token(client, shop, lists, s3):
    draft = _draft(shop)

    added = _add(client, shop, new_supplier_token=_token(shop))

    assert added.status_code == 201, added.content
    assert not XeroContactSync.objects.filter(name="Corner Bakery").exists()
    held = XeroContactSync.objects.get(xero_contact_id="con-0001")
    assert ReportExpense.objects.get(report_id=draft.id).contact_id == held.id


def test_an_edit_takes_a_read_supplier_too(client, shop, lists, s3):
    draft = _draft(shop)
    line, _ = _line(shop, draft, s3)

    edited = _edit(client, shop, line.id, new_supplier_token=_token(shop))

    assert edited.status_code == 200, edited.content
    made = XeroContactSync.objects.get(entity_id=shop.entity.id, name="Corner Bakery")
    line.refresh_from_db()
    assert line.contact_id == made.id


def test_an_edit_with_a_bad_reader_token_changes_nothing(client, shop, lists, s3):
    draft = _draft(shop)
    line, _ = _line(shop, draft, s3)

    edited = _edit(client, shop, line.id, new_supplier_token="forged.token", amount="5")

    assert edited.status_code == 422
    line.refresh_from_db()
    assert line.amount == Decimal("1012.50")
    assert not XeroContactSync.objects.filter(name="Corner Bakery").exists()


# ---- the duplicate reminder (owner's decisions doc DUP1, 2026-11-19) ---------------------------------


def test_the_same_day_supplier_and_amount_asks_first_and_saves_when_told(client, shop, lists, s3):
    """DUP1: another line with the same date (the report's day), contact and exactly the same amount
    is asked about before the save; "save anyway" always saves - it never blocks."""
    draft = _draft(shop)
    assert _add(client, shop).status_code == 201

    again = _add(client, shop, description="Flyers again")
    assert again.status_code == 409
    body = again.json()
    assert body["error"] == ("This looks like an expense you already have on this day: the same supplier and "
                             "amount. Save it anyway?")
    assert [(d["description"], d["amount"], d["supplier"]) for d in body["duplicates"]] == [
        ("Flyers", "1012.50", "ABC Supplies")]
    assert ReportExpense.objects.filter(report_id=draft.id).count() == 1, "nothing saved yet"

    anyway = _add(client, shop, description="Flyers again", confirm_duplicate="true")
    assert anyway.status_code == 201
    assert ReportExpense.objects.filter(report_id=draft.id).count() == 2


def test_another_amount_or_supplier_is_no_duplicate(client, shop, lists, s3):
    _draft(shop)
    assert _add(client, shop).status_code == 201
    assert _add(client, shop, amount="1,012.51").status_code == 201
    assert _add(client, shop, supplier_id="con-0002").status_code == 201


def test_an_edit_that_makes_a_duplicate_asks_too_and_a_line_is_never_its_own(client, shop, lists, s3):
    draft = _draft(shop)
    _line(shop, draft, s3)
    other, _attachment = _line(shop, draft, s3, amount="5.00")
    asked = _edit(client, shop, other.id, amount="1012.50")
    assert asked.status_code == 409 and asked.json()["duplicates"]
    assert ReportExpense.objects.get(id=other.id).amount == Decimal("5.00")
    told = _edit(client, shop, other.id, amount="1012.50", confirm_duplicate="true")
    assert told.status_code == 200
    lone, _attachment = _line(shop, draft, s3, amount="7.00")
    assert _edit(client, shop, lone.id, description="New words").status_code == 200
