from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from treinvertraging import crypto, db
from treinvertraging.models import Account, SettingKV

pytest_plugins = ["tests.unit.test_web_support"]

VALID_IBAN = "NL91 ABNA 0417 1643 00"

FORM = {
    "source": "ns",
    "label": "Lucas NS",
    "username": "lucas@example.com",
    "password": "hunter2-very-secret",
    "card_number": "3528 0123 4567 8901",
    "iban": VALID_IBAN,
    "holder_name": "L. Habets",
    "email": "lucas@example.com",
    "otp_provider": "manual",
    "enabled": "1",
}


def _accounts() -> list[Account]:
    with db.session_scope() as s:
        return list(s.scalars(select(Account)))


def test_create_account_stores_secrets_encrypted(client: TestClient) -> None:
    r = client.post("/accounts", data=FORM)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/accounts?")
    (acc,) = _accounts()
    assert acc.label == "Lucas NS"
    assert acc.username == "lucas@example.com"
    assert acc.enabled is True
    for enc, plain in (
        (acc.password_enc, "hunter2-very-secret"),
        (acc.card_number_enc, "3528012345678901"),
        (acc.iban_enc, "NL91ABNA0417164300"),
    ):
        assert enc and plain not in enc
        assert crypto.decrypt(enc) == plain


def test_account_list_masks_secrets(client: TestClient) -> None:
    client.post("/accounts", data=FORM)
    page = client.get("/accounts").text
    assert "Lucas NS" in page
    assert "hunter2-very-secret" not in page
    assert "3528012345678901" not in page
    assert "ABNA" not in page
    assert "8901" in page and "4300" in page


def test_create_validation_errors(client: TestClient) -> None:
    bad = {**FORM, "iban": "NL00 BANK 0000 0000 00", "card_number": "123", "password": ""}
    r = client.post("/accounts", data=bad)
    assert r.status_code == 422
    assert "IBAN" in r.text and "16 cijfers" in r.text and "Wachtwoord is verplicht" in r.text
    assert _accounts() == []
    assert "hunter2" not in r.text


def test_edit_keeps_secrets_when_blank(client: TestClient) -> None:
    client.post("/accounts", data=FORM)
    (acc,) = _accounts()
    old = (acc.password_enc, acc.card_number_enc, acc.iban_enc)
    edit = {**FORM, "password": "", "card_number": "", "iban": "", "label": "Nieuw label"}
    del edit["enabled"]  # unchecked checkbox
    r = client.post(f"/accounts/{acc.id}", data=edit)
    assert r.status_code == 303
    (acc,) = _accounts()
    assert acc.label == "Nieuw label"
    assert acc.enabled is False
    assert (acc.password_enc, acc.card_number_enc, acc.iban_enc) == old

    client.post(f"/accounts/{acc.id}", data={**FORM, "password": "changed-pw"})
    (acc,) = _accounts()
    assert crypto.decrypt(acc.password_enc) == "changed-pw"


def test_edit_form_prefills_without_secrets(client: TestClient) -> None:
    client.post("/accounts", data=FORM)
    (acc,) = _accounts()
    page = client.get(f"/accounts/{acc.id}/edit").text
    assert "lucas@example.com" in page
    assert "hunter2" not in page
    assert client.get("/accounts/999/edit").status_code == 404


def test_test_login_and_calibrate_stub_show_flash(client: TestClient) -> None:
    client.post("/accounts", data=FORM)
    (acc,) = _accounts()
    r = client.post(f"/accounts/{acc.id}/test-login")
    assert r.status_code == 303
    assert "nog+niet+beschikbaar" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "nog niet beschikbaar" in page
    r = client.post(f"/accounts/{acc.id}/calibrate", data={"channel": "gtbv"})
    assert "nog+niet+beschikbaar" in r.headers["location"]
    r = client.post(f"/accounts/{acc.id}/calibrate", data={"channel": "bogus"})
    assert "Onbekend" in client.get(r.headers["location"]).text


def test_test_login_calls_service(client: TestClient, monkeypatch) -> None:
    from treinvertraging import services

    seen: list[int] = []

    async def fake(account_id: int) -> str:
        seen.append(account_id)
        return "Ingelogd als test"

    monkeypatch.setattr(services, "test_login", fake)
    client.post("/accounts", data=FORM)
    (acc,) = _accounts()
    r = client.post(f"/accounts/{acc.id}/test-login", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Ingelogd als test" in r.text and 'class="flash success"' in r.text
    assert seen == [acc.id]
    assert "niet gevonden" in client.post("/accounts/999/test-login", headers={"HX-Request": "true"}).text


def test_imap_settings_saved_encrypted(client: TestClient) -> None:
    r = client.post(
        "/settings/imap",
        data={"host": "imap.example.com", "port": "993", "user": "me", "password": "app-pw",
              "folder": "INBOX", "senders": "ns.nl", "code_regex": r"\b\d{6}\b"},
    )  # fmt: skip
    assert r.status_code == 303
    with db.session_scope() as s:
        rows = {k.key: k.value for k in s.scalars(select(SettingKV))}
    assert rows["imap.host"] == "imap.example.com"
    assert "app-pw" not in rows["imap.password_enc"]
    assert crypto.decrypt(rows["imap.password_enc"]) == "app-pw"
    assert "imap.example.com" in client.get("/accounts").text
    bad = client.post("/settings/imap", data={"code_regex": "("})
    assert "Ongeldige" in client.get(bad.headers["location"]).text
