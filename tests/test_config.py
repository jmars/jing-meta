"""Tests for search.config — domain config validation."""

import pytest

from search.config import Config, ConfigError, DomainConfig, load_config

# --- DomainConfig direct construction ---

def test_valid_defaults_pass():
    dc = DomainConfig(name="d", dir=".")
    assert dc.type == "files"
    assert dc.extractor == "jsonl"
    assert dc.effective_renderer == "jsonl"


def test_valid_explicit():
    dc = DomainConfig(name="d", dir=".", type="dirs", extractor="txt")
    assert dc.type == "dirs"
    assert dc.extractor == "txt"


def test_invalid_type_raises():
    with pytest.raises(ConfigError, match="invalid type 'bad'"):
        DomainConfig(name="d", dir=".", type="bad")


def test_valid_extractor_notification():
    """'notification' is a valid config extractor for non-DAFSA domains."""
    dc = DomainConfig(name="d", dir=".", extractor="notification")
    assert dc.extractor == "notification"


def test_invalid_extractor_raises():
    with pytest.raises(ConfigError, match="invalid extractor 'bogus'"):
        DomainConfig(name="d", dir=".", extractor="bogus")


def test_empty_renderer_valid_effective_equals_extractor():
    dc = DomainConfig(name="d", dir=".", extractor="txt")
    assert dc.renderer == ""
    assert dc.effective_renderer == "txt"


def test_nonempty_renderer_txt_valid():
    dc = DomainConfig(name="d", dir=".", extractor="jsonl", renderer="txt")
    assert dc.effective_renderer == "txt"


def test_invalid_renderer_raises():
    with pytest.raises(ConfigError, match="invalid renderer 'bad'"):
        DomainConfig(name="d", dir=".", renderer="bad")


def test_empty_pattern_raises():
    with pytest.raises(ConfigError, match="pattern must not be empty"):
        DomainConfig(name="d", dir=".", pattern="")


def test_whitespace_pattern_raises():
    with pytest.raises(ConfigError, match="pattern must not be empty"):
        DomainConfig(name="d", dir=".", pattern="   ")


def test_valid_bracket_pattern_passes():
    dc = DomainConfig(name="d", dir=".", pattern="[a-z]*")
    assert dc.pattern == "[a-z]*"


def test_empty_fst_pattern_valid():
    dc = DomainConfig(name="d", dir=".")
    assert dc.fst_pattern == ""
    assert dc.effective_index_dir == dc.dir


def test_whitespace_fst_pattern_raises():
    with pytest.raises(ConfigError, match="fst_pattern must not be empty"):
        DomainConfig(name="d", dir=".", fst_pattern="   ")


def test_effective_renderer_fallback():
    dc = DomainConfig(name="d", dir=".", extractor="txt")
    assert dc.effective_renderer == "txt"
    dc2 = DomainConfig(name="d", dir=".", extractor="jsonl", renderer="transcript")
    assert dc2.effective_renderer == "transcript"


# --- load_config TOML-based ---

def test_no_file_empty_config(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.toml")
    assert isinstance(cfg, Config)
    assert cfg.domains == {}
    assert cfg.history_file is None
    assert cfg.log_file is None


def test_valid_toml_loads_two_domains(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[domains.a]\n"
        "dir = '/tmp/x'\n"
        "type = 'files'\n"
        "extractor = 'jsonl'\n"
        "[domains.b]\n"
        "dir = '/tmp/y'\n"
        "type = 'dirs'\n"
        "extractor = 'txt'\n"
    )
    cfg = load_config(p)
    assert set(cfg.domains) == {"a", "b"}
    assert cfg.domains["a"].type == "files"
    assert cfg.domains["a"].extractor == "jsonl"
    assert cfg.domains["b"].type == "dirs"
    assert cfg.domains["b"].extractor == "txt"


def test_invalid_type_in_toml_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[domains.a]\ndir = '/tmp/x'\ntype = 'unknown'\n")
    with pytest.raises(ConfigError, match="invalid type 'unknown'"):
        load_config(p)


def test_invalid_extractor_in_toml_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[domains.a]\ndir = '/tmp/x'\nextractor = 'bogus'\n")
    with pytest.raises(ConfigError, match="invalid extractor 'bogus'"):
        load_config(p)


def test_notification_extractor_in_toml_loads(tmp_path):
    """A notifications domain with extractor='notification' must load from TOML."""
    p = tmp_path / "config.toml"
    p.write_text("[domains.notifications]\ndir = '/tmp/x'\nextractor = 'notification'\n")
    cfg = load_config(p)
    assert cfg.domains["notifications"].extractor == "notification"


def test_empty_pattern_in_toml_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[domains.a]\ndir = '/tmp/x'\npattern = ''\n")
    with pytest.raises(ConfigError, match="pattern must not be empty"):
        load_config(p)


def test_unknown_extra_keys_ignored(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[domains.a]\ndir = '/tmp/x'\nunknown_key = 42\nother = 'zzz'\n")
    cfg = load_config(p)
    assert "a" in cfg.domains
    assert cfg.domains["a"].type == "files"


def test_history_and_log_sections(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[history]\nfile = '/tmp/hist.jsonl'\n"
        "[log]\nfile = '/tmp/log.txt'\n"
    )
    cfg = load_config(p)
    assert cfg.history_file is not None
    assert str(cfg.history_file) == "/tmp/hist.jsonl"
    assert cfg.log_file is not None
    assert str(cfg.log_file) == "/tmp/log.txt"
