from treinvertraging import selectors


def test_user_file_overrides_bundled_defaults(data_dir, monkeypatch, tmp_path):
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "demo.yaml").write_text("a: {b: 1, c: 2}\nlist: [1]\n")
    monkeypatch.setattr(selectors, "DEFAULTS_DIR", defaults)
    selectors.reset_cache()
    assert selectors.get("demo", "a.c") == 2

    user_dir = selectors.get_settings().CONFIG_DIR / "selectors"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "demo.yaml").write_text("a: {c: 3}\n")
    assert selectors.get("demo", "a.b") == 1
    assert selectors.get("demo", "a.c") == 3
    assert selectors.get("demo", "missing.key", "x") == "x"


def test_unknown_site_is_empty(data_dir):
    selectors.reset_cache()
    assert selectors.load_selectors("does-not-exist") == {}
