from __future__ import annotations

import pytest

from tests.unit.pipeline_support import (
    add_account,
    env,  # noqa: F401 - fixture
)
from treinvertraging import crypto, db
from treinvertraging.adapters import registry
from treinvertraging.adapters.fake import FakeStatusReader, FakeSubmitter, FakeTripSource
from treinvertraging.domain import Source
from treinvertraging.models import Account, SettingKV
from treinvertraging.otp.imap import ImapConfig, ImapOtpProvider
from treinvertraging.otp.manual import ManualOtpProvider
from treinvertraging.pipeline.context import load_imap_config


def account(acc_id: int) -> Account:
    with db.session_scope() as s:
        return s.get(Account, acc_id)


def set_kv(**values: str) -> None:
    with db.session_scope() as s:
        for k, v in values.items():
            s.add(SettingKV(key=f"imap.{k}", value=v))


def test_real_adapters_are_built_lazily_per_source(env):  # noqa: F811
    from treinvertraging.adapters.gtbv import GtbvSubmitter
    from treinvertraging.adapters.ns import NsAdapter
    from treinvertraging.adapters.ovchip import OvchipAdapter

    ns = account(add_account(Source.NS))
    ov = account(add_account(Source.OVCHIP))
    assert isinstance(registry.trip_source_for(ns), NsAdapter)
    assert isinstance(registry.trip_source_for(ov), OvchipAdapter)
    subs = registry.submitters(["gtbv", "mijnns", "bogus"], account=ov)
    assert [type(s) for s in subs] == [GtbvSubmitter, NsAdapter]
    assert [s.channel for s in subs] == ["gtbv", "mijnns"]
    assert subs[1].account.id == ns.id  # an OV trip falls back to the first enabled NS account
    readers = registry.status_readers([ns, ov])
    assert len(readers) == 1 and isinstance(readers[0], NsAdapter)


def test_mijnns_channel_skipped_without_ns_account(env):  # noqa: F811
    add_account(Source.OVCHIP)
    assert [s.channel for s in registry.submitters(["mijnns", "gtbv"])] == ["gtbv"]


def test_fake_adapters_selected_by_env(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TV_FAKE_ADAPTERS", "1")
    monkeypatch.setenv("TV_FAKE_FAIL", "gtbv:blocked, mijnns:layout")
    acc = account(add_account(Source.OVCHIP))
    src = registry.trip_source_for(acc)
    assert isinstance(src, FakeTripSource) and src.source is Source.OVCHIP and len(src.trips) == 2
    subs = registry.submitters(["gtbv", "mijnns"])
    assert all(isinstance(s, FakeSubmitter) for s in subs)
    assert [type(s.always).__name__ for s in subs] == ["BlockedByBotProtection", "LayoutChanged"]
    assert isinstance(registry.status_readers([acc])[0], FakeStatusReader)


def test_fake_trips_can_come_from_a_json_file(env, monkeypatch, tmp_path):  # noqa: F811
    path = tmp_path / "trips.json"
    path.write_text(
        '[{"card_id": "c1", "checkin_at": "2026-10-20T08:15:00", "from_station": "UT", "to_station": "ASD",'
        ' "ns_delay_flag": true, "fare_eur": "9.50"}]'
    )
    monkeypatch.setenv("TV_FAKE_ADAPTERS", "1")
    monkeypatch.setenv("TV_FAKE_TRIPS_FILE", str(path))
    (trip,) = registry.trip_source_for(account(add_account())).trips
    assert trip.source is Source.NS and trip.checkin_at.tzinfo is not None and str(trip.fare_eur) == "9.50"


def test_imap_config_prefers_settings_over_env_and_decrypts(env, monkeypatch):  # noqa: F811
    assert load_imap_config() is None
    monkeypatch.setenv("IMAP_HOST", "env.example.com")
    monkeypatch.setenv("IMAP_USER", "env-user")
    monkeypatch.setenv("IMAP_PASSWORD", "env-pw")
    cfg = load_imap_config()
    assert (cfg.host, cfg.user, cfg.password) == ("env.example.com", "env-user", "env-pw")
    set_kv(
        host="imap.kv.example.com",
        port="143",
        user="kv-user",
        password_enc=crypto.encrypt("kv-pw"),
        folder="Treinen",
        senders="ns.nl, noreply@ov-chipkaart.nl",
        code_regex=r"code (\d{6})",
    )
    cfg = load_imap_config()
    assert isinstance(cfg, ImapConfig)
    assert (cfg.host, cfg.port, cfg.user, cfg.password, cfg.folder) == (
        "imap.kv.example.com",
        143,
        "kv-user",
        "kv-pw",
        "Treinen",
    )
    assert cfg.senders == ("ns.nl", "noreply@ov-chipkaart.nl") and cfg.code_regex == r"code (\d{6})"


def test_incomplete_or_undecryptable_settings_fall_back(env, monkeypatch):  # noqa: F811
    set_kv(host="imap.kv.example.com", user="u", password_enc="not-a-fernet-token")
    assert load_imap_config() is None
    monkeypatch.setenv("IMAP_HOST", "env.example.com")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    assert load_imap_config().host == "env.example.com"


@pytest.mark.parametrize("configured", [True, False])
def test_imap_otp_provider_only_when_configured(env, configured):  # noqa: F811
    from treinvertraging.adapters.ns import NsAdapter

    if configured:
        set_kv(host="h", user="u", password_enc=crypto.encrypt("p"))
    acc = account(add_account(otp="imap"))
    adapter = registry.trip_source_for(acc)
    assert isinstance(adapter, NsAdapter)
    expected = ImapOtpProvider if configured else ManualOtpProvider
    assert isinstance(adapter._otp, expected)
