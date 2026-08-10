"""Tests for the index-hook (jing_meta/hooks/indexhook.py).

Covers the ``run_updates`` core (DAFSA-capability filtering, result plumbing)
and ``main``'s opt-out env path. ``update_index`` is monkeypatched so no DAFSA
C lib or real index build is required.
"""

import json
from pathlib import Path

from jing_meta.hooks import indexhook
from search.config import load_config


def _write_domain_toml(tmp_path, domains: dict) -> Path:
    """Write a minimal TOML config describing *domains* and return its path."""
    lines = []
    for name, d in domains.items():
        lines.append(f"[domains.{name}]")
        for k, v in d.items():
            lines.append(f"{k} = {json.dumps(v)}")
    cfg_path = tmp_path / "unified-history.toml"
    cfg_path.write_text("\n".join(lines), encoding="utf-8")
    return cfg_path


def _load(tmp_path, domains: dict):
    return load_config(_write_domain_toml(tmp_path, domains))


def _jsonl_domain(tmp_path, name) -> dict:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "a.jsonl").write_text(json.dumps({"content": "alpha"}), encoding="utf-8")
    return {"dir": str(d), "extractor": "jsonl", "pattern": "*.jsonl"}


def test_run_updates_skips_non_dafsa_domains(tmp_path, monkeypatch):
    cfg = _load(tmp_path, {
        "j": _jsonl_domain(tmp_path, "j"),
        "n": {"dir": str(tmp_path), "extractor": "notification"},
    })
    calls = []

    def fake_update(domain_cfg):
        calls.append(domain_cfg)
        return (True, "ok")

    monkeypatch.setattr(indexhook, "update_index", fake_update)
    indexhook.run_updates(cfg)
    assert [c.name for c in calls] == ["j"]


def test_run_updates_calls_all_dafsa_domains(tmp_path, monkeypatch):
    cfg = _load(tmp_path, {
        "a": _jsonl_domain(tmp_path, "a"),
        "b": _jsonl_domain(tmp_path, "b"),
        "c": _jsonl_domain(tmp_path, "c"),
    })
    calls = []

    def fake_update(domain_cfg):
        calls.append(domain_cfg.name)
        return (True, "ok")

    monkeypatch.setattr(indexhook, "update_index", fake_update)
    indexhook.run_updates(cfg)
    assert sorted(calls) == ["a", "b", "c"]


def test_run_updates_empty_config_returns_empty(tmp_path, monkeypatch):
    cfg = load_config(tmp_path / "does-not-exist.toml")
    assert not cfg.domains

    def fake_update(domain_cfg):
        raise AssertionError("should not be called")

    monkeypatch.setattr(indexhook, "update_index", fake_update)
    assert indexhook.run_updates(cfg) == []


def test_run_updates_returns_results(tmp_path, monkeypatch):
    cfg = _load(tmp_path, {"d": _jsonl_domain(tmp_path, "d")})

    def fake_update(domain_cfg):
        return (True, "msg")

    monkeypatch.setattr(indexhook, "update_index", fake_update)
    assert indexhook.run_updates(cfg) == [("d", True, "msg")]


def test_main_disabled_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JING_INDEX_HOOK_ENABLED", "0")

    def boom(*args, **kwargs):
        raise AssertionError("update_index must not run when disabled")

    monkeypatch.setattr(indexhook, "update_index", boom)
    assert indexhook.main() == 0
