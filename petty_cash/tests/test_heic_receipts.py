"""iPhone HEIC receipts (the user, 2026-11-24): stored exactly as uploaded; a JPEG is made on the
fly only where HEIC can't be used - the receipt route's ``?as=jpeg`` (a browser that can't draw
it) and the Xero publish (Xero takes no HEIC). No copy is ever stored."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from minty_db.models import Attachment, ReportExpense
from PIL import Image

from petty_cash.tests import test_expenses as expenses_tests
from petty_cash.tests.conftest import headers, url
from petty_cash.tests.test_expenses import D3, PNG, _draft, _line
from petty_cash.xero import receipts

pytestmark = pytest.mark.django_db

# the Expenses step's fixtures: an in-memory bucket and the company's lists
s3 = expenses_tests.s3
lists = expenses_tests.lists

#: 80x40 stored with EXIF orientation 6: upright 40x80 (minty-shared-py's fixture).
HEIC = (Path(__file__).parent / "fixtures" / "tiny.heic").read_bytes()
NOT_A_PHOTO = b"\x00\x00\x00\x18ftypheic but nothing a decoder can read"


def _jpeg_size(data: bytes) -> tuple[str, tuple[int, int]]:
    with Image.open(io.BytesIO(data)) as img:
        return img.format, img.size


def _add_heic(client, shop, data=HEIC, name="IMG_1234.HEIC", content_type="image/heic"):
    form = {"transaction_date": D3.isoformat(), "amount": "12.40", "description": "Lunch",
            "supplier_id": "con-0001", "account_id": "acc-0001",
            "receipt": SimpleUploadedFile(name, data, content_type=content_type)}
    return client.post(url(shop.entity.id, "/reports/expenses/lines"), form, headers=headers(shop.owner.id))


# --- upload ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("content_type", ["image/heic", "application/octet-stream", ""])
def test_a_heic_receipt_is_stored_as_an_upright_jpeg(client, shop, lists, s3, content_type):
    """2026-11-26 (the user): B2 keeps photos compressed, in a type Xero takes - a HEIC is a .jpg."""
    _draft(shop)

    resp = _add_heic(client, shop, content_type=content_type)

    assert resp.status_code == 201, resp.content
    [key] = s3.objects
    stored = s3.objects[key]
    assert _jpeg_size(stored) == ("JPEG", (40, 80)) and key.endswith(".jpg")
    assert s3.extra_args == {"ContentType": "image/jpeg"}
    row = Attachment.objects.get(file_path=key)
    assert (row.mime_type, row.file_extension, row.file_size) == ("image/jpeg", "jpg", len(stored))
    assert row.original_name == "IMG_1234.jpg"
    assert resp.json()["line"]["receipts"][0]["mime_type"] == "image/jpeg"


def test_a_heic_that_will_not_open_is_refused_and_nothing_is_stored(client, shop, lists, s3):
    draft = _draft(shop)

    resp = _add_heic(client, shop, data=NOT_A_PHOTO)

    assert resp.status_code == 422
    assert resp.json()["error"] == "This photo can't be opened. Attach it again as a JPEG or PNG."
    assert s3.objects == {} and not ReportExpense.objects.filter(report_id=draft.id).exists()


# --- showing it --------------------------------------------------------------------------------------


def _stored_heic(shop, s3):
    draft = _draft(shop)
    line, attachment = _line(shop, draft, s3, receipt=HEIC, name="IMG_1234.HEIC")
    return line, attachment


def test_a_heic_receipt_comes_back_as_it_is_for_a_browser_that_draws_it(client, shop, lists, s3):
    line, attachment = _stored_heic(shop, s3)

    resp = client.get(url(shop.entity.id, f"/expense-lines/{line.id}/receipts/{attachment.id}"),
                      headers=headers(shop.owner.id))

    assert resp.status_code == 200
    assert resp.content == HEIC and resp["Content-Type"] == "image/heic"


def test_as_jpeg_gives_an_upright_jpeg_made_on_the_fly_and_stores_nothing(client, shop, lists, s3):
    line, attachment = _stored_heic(shop, s3)
    before = dict(s3.objects)

    resp = client.get(url(shop.entity.id, f"/expense-lines/{line.id}/receipts/{attachment.id}"), {"as": "jpeg"},
                      headers=headers(shop.owner.id))

    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/jpeg"
    assert _jpeg_size(resp.content) == ("JPEG", (40, 80))
    assert resp["X-Content-Type-Options"] == "nosniff" and resp["Content-Disposition"] == "inline"
    assert s3.objects == before  # nothing written


def test_as_jpeg_leaves_any_other_receipt_exactly_as_it_is(client, shop, lists, s3):
    draft = _draft(shop)
    line, attachment = _line(shop, draft, s3, receipt=PNG)

    resp = client.get(url(shop.entity.id, f"/expense-lines/{line.id}/receipts/{attachment.id}"), {"as": "jpeg"},
                      headers=headers(shop.owner.id))

    assert resp.status_code == 200 and resp.content == PNG and resp["Content-Type"] == "image/png"


def test_a_stored_heic_that_no_longer_opens_is_a_422_not_a_crash(client, shop, lists, s3):
    draft = _draft(shop)
    line, attachment = _line(shop, draft, s3, receipt=NOT_A_PHOTO, name="IMG_1.HEIC")

    resp = client.get(url(shop.entity.id, f"/expense-lines/{line.id}/receipts/{attachment.id}"), {"as": "jpeg"},
                      headers=headers(shop.owner.id))

    assert resp.status_code == 422
    assert resp.json()["error"] == "This photo can't be shown here."


# --- publishing it -----------------------------------------------------------------------------------


class _Answer:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, ""

    def json(self):
        return self._body


class _FilesApi:
    """Xero's Files API, recording what was uploaded: ``(name, bytes, content type)``."""

    def __init__(self):
        self.uploaded: list[tuple[str, bytes, str]] = []

    def get(self, url, **_):
        return _Answer(404, [])

    def post(self, url, files=None, json=None, **_):
        if files:
            [(part, (name, data, content_type))] = files.items()
            assert part == name
            self.uploaded.append((name, data, content_type))
            return _Answer(201, {"FileId": "file-1"})
        return _Answer(200, {})

    def delete(self, url, **_):
        return _Answer(204, None)


@pytest.fixture
def files_api(monkeypatch):
    fake = _FilesApi()
    for verb in ("get", "post", "delete"):
        monkeypatch.setattr(receipts.requests, verb, getattr(fake, verb))
    return fake


def _publish(monkeypatch, key: str, data: bytes) -> None:
    monkeypatch.setattr(receipts, "first_receipt_key", lambda line: key)
    monkeypatch.setattr(receipts.storage, "read_receipt", lambda k, limit: data)
    run = SimpleNamespace(access_token="tok", org="org-1")
    line = SimpleNamespace(id="line-1", description="Lunch", item="Meals")
    assert receipts.attach_receipt(run, line, "btx-1") is True


def test_a_heic_receipt_goes_to_xero_as_a_jpeg(monkeypatch, files_api):
    _publish(monkeypatch, "expenses/r/23_NOV_2026_MEALS_12.heic", HEIC)

    [(name, data, content_type)] = files_api.uploaded
    assert (name, content_type) == ("LUNCH.jpg", "image/jpeg")
    assert _jpeg_size(data) == ("JPEG", (40, 80))


def test_a_jpeg_or_pdf_receipt_goes_to_xero_unchanged(monkeypatch, files_api):
    _publish(monkeypatch, "expenses/r/23_NOV_2026_MEALS_12.pdf", b"%PDF-1.7 a receipt")

    assert files_api.uploaded == [("LUNCH.pdf", b"%PDF-1.7 a receipt", "application/pdf")]


def test_an_unchanged_heic_on_xero_is_left_alone(monkeypatch, files_api):
    """The JPEG made for Xero is the same every time, so a republish sees Xero already holds it."""
    from minty_shared import images

    sent = images.to_jpeg(HEIC)
    monkeypatch.setattr(receipts.requests, "get", lambda url, **_: _Answer(
        200, [{"FileId": "file-old", "Name": "LUNCH.jpg", "Size": len(sent)}]))

    _publish(monkeypatch, "expenses/r/23_NOV_2026_MEALS_12.heic", HEIC)

    assert files_api.uploaded == []
