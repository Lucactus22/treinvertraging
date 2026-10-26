from __future__ import annotations

import imaplib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from treinvertraging.otp import imap as imap_mod
from treinvertraging.otp.base import OtpProvider, OtpTimeout, get_otp_provider
from treinvertraging.otp.imap import (
    ImapConfig,
    ImapOtpProvider,
    extract_code,
    find_code,
    parse_message,
)

FIX = Path(__file__).parent.parent / "fixtures" / "otp"
SINCE = datetime(2026, 10, 26, 8, 59, tzinfo=UTC)  # 09:59 +0100


def eml(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_parse_plain_ns_mail() -> None:
    m = parse_message(eml("ns_otp_plain.eml"), uid="1")
    assert m.sender == "no-reply@ns.nl"
    assert m.date == datetime(2026, 10, 26, 9, 0, 5, tzinfo=UTC)
    assert extract_code(m) == "482913"  # not the 4-digit postcode / "2025"


def test_parse_html_quoted_printable_ignores_css_colors() -> None:
    m = parse_message(eml("ov_otp_html.eml"))
    assert m.sender == "noreply@ov-chipkaart.nl"
    assert "735190" in m.text
    assert extract_code(m) == "735190"


def test_code_in_subject_and_custom_regex_group() -> None:
    m = parse_message(eml("ns_otp_code_in_subject.eml"))
    assert extract_code(m) == "654321"
    assert extract_code(parse_message(eml("ns_confirmation.eml")), r"GTBV-(\d{4}-\d{6})") == "2026-445566"
    assert extract_code(parse_message(eml("ns_confirmation.eml"))) == "445566"  # \b boundaries: after hyphen


def test_find_code_filters_sender_and_age_and_prefers_newest() -> None:
    mails = [parse_message(eml(n)) for n in ("ns_otp_old.eml", "other_sender.eml", "ns_otp_plain.eml", "ov_otp_html.eml")]
    assert find_code(mails, since=SINCE) == "735190"  # newest eligible: 10:02:30
    only_ns = find_code(mails, since=SINCE, senders=("@ns.nl",))
    assert only_ns == "482913"
    assert find_code(mails, since=SINCE + timedelta(hours=2)) is None
    assert find_code([mails[1]], since=SINCE) is None  # sender filtered even though it has 6 digits


def test_find_code_naive_since_is_utc() -> None:
    m = parse_message(eml("ns_otp_plain.eml"))
    assert find_code([m], since=datetime(2026, 10, 26, 8, 59)) == "482913"


class FakeImap:
    """Stands in for imaplib.IMAP4_SSL; serves the given raw messages."""

    instances: list[FakeImap] = []
    messages: list[bytes] = []
    fail_times = 0

    def __init__(self, host: str, port: int = 993, timeout: float | None = None) -> None:
        if FakeImap.fail_times > 0:
            FakeImap.fail_times -= 1
            raise OSError("connection refused")
        self.host, self.port = host, port
        self.selected: tuple[str, bool] | None = None
        self.search_args: tuple = ()
        self.logged_out = False
        FakeImap.instances.append(self)

    def login(self, user: str, password: str):
        assert (user, password) == ("me@example.com", "app-pass")
        return "OK", [b"ok"]

    def select(self, folder: str, readonly: bool = False):
        self.selected = (folder, readonly)
        return "OK", [b"3"]

    def search(self, charset, *criteria):
        self.search_args = criteria
        ids = b" ".join(str(i + 1).encode() for i in range(len(FakeImap.messages)))
        return "OK", [ids]

    def fetch(self, mid: bytes, spec: str):
        assert "PEEK" in spec  # must not mark mails as read
        return "OK", [(b"1 (BODY[] {10}", FakeImap.messages[int(mid) - 1]), b")"]

    def logout(self):
        self.logged_out = True


@pytest.fixture
def fake_imap(monkeypatch: pytest.MonkeyPatch) -> type[FakeImap]:
    FakeImap.instances = []
    FakeImap.messages = []
    FakeImap.fail_times = 0
    monkeypatch.setattr(imap_mod.imaplib, "IMAP4_SSL", FakeImap)
    return FakeImap


def cfg(**kw) -> ImapConfig:
    return ImapConfig(host="imap.example.com", user="me@example.com", password="app-pass", poll_interval=0.05, **kw)


async def test_get_code_via_mocked_imaplib(fake_imap: type[FakeImap]) -> None:
    fake_imap.messages = [eml("ns_otp_old.eml"), eml("other_sender.eml"), eml("ns_otp_plain.eml")]
    p = ImapOtpProvider(cfg())
    assert isinstance(p, OtpProvider)
    code = await p.get_code("acc", SINCE, timeout=2)
    assert code == "482913"
    inst = fake_imap.instances[0]
    assert inst.selected == ("INBOX", True)  # read-only
    assert inst.search_args[0] == "SINCE"
    assert inst.search_args[1] == "24-Oct-2026" or inst.search_args[1] == "25-Oct-2026"
    assert inst.logged_out


async def test_get_code_retries_after_connection_error(fake_imap: type[FakeImap]) -> None:
    fake_imap.messages = [eml("ns_otp_plain.eml")]
    fake_imap.fail_times = 2
    code = await ImapOtpProvider(cfg()).get_code("acc", SINCE, timeout=3)
    assert code == "482913"


async def test_get_code_times_out(fake_imap: type[FakeImap]) -> None:
    fake_imap.messages = [eml("ns_otp_old.eml")]
    with pytest.raises(OtpTimeout):
        await ImapOtpProvider(cfg()).get_code("acc", SINCE, timeout=0.3)


async def test_list_messages_confirmation_mail(fake_imap: type[FakeImap]) -> None:
    fake_imap.messages = [eml("ns_otp_plain.eml"), eml("ns_confirmation.eml")]
    mails = await ImapOtpProvider(cfg()).list_messages(SINCE, subject_regex=r"aanvraag.*ontvangen")
    assert [m.sender for m in mails] == ["klantenservice@ns.nl"]
    assert "GTBV-2026-445566" in mails[0].text


def test_config_from_env_and_factory() -> None:
    env = {
        "IMAP_HOST": "h",
        "IMAP_USER": "u",
        "IMAP_PASSWORD": "s3cr3t-pw",
        "IMAP_SENDERS": "ns.nl, example.org",
        "IMAP_CODE_REGEX": r"code: (\d+)",
        "IMAP_PORT": "143",
        "IMAP_SSL": "false",
    }
    c = ImapConfig.from_env(env)
    assert (c.host, c.port, c.ssl, c.senders, c.code_regex) == ("h", 143, False, ("ns.nl", "example.org"), r"code: (\d+)")
    assert "s3cr3t-pw" not in repr(c)  # password never shows up in logs
    with pytest.raises(ValueError):
        ImapConfig.from_env({})

    class Acc:
        otp_provider = "imap"

    assert isinstance(get_otp_provider(Acc(), imap=c), ImapOtpProvider)

    class Acc2:
        otp_provider = "manual"

    from treinvertraging.otp.manual import ManualOtpProvider

    assert isinstance(get_otp_provider(Acc2()), ManualOtpProvider)


def test_imap_error_class_is_caught_type() -> None:
    assert issubclass(imaplib.IMAP4.error, Exception)
