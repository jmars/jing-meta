"""Domain configuration — loaded from a TOML config file."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# tomllib is stdlib in Python 3.11+; fall back to tomli for 3.10
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore


@dataclass
class DomainConfig:
    """Configuration for a single searchable domain."""

    name: str
    dir: Path
    pattern: str = "*"
    type: str = "files"  # "files" or "dirs"
    extensions: list[str] = field(default_factory=list)
    extractor: str = "jsonl"
    renderer: str = ""
    label: str = "file"
    fst_binary: str = "fst-indexer"
    fst_pattern: str = ""
    fst_index_dir: Optional[str] = None
    filters: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Resolve paths with ~ expansion and absolute conversion."""
        if isinstance(self.dir, str):
            self.dir = Path(self.dir).expanduser().resolve()
        elif isinstance(self.dir, Path):
            self.dir = self.dir.expanduser().resolve()
        if self.fst_index_dir:
            self.fst_index_dir = str(Path(self.fst_index_dir).expanduser().resolve())

    @property
    def effective_renderer(self) -> str:
        """Return the renderer name, defaulting to the extractor name."""
        return self.renderer or self.extractor

    @property
    def effective_index_dir(self) -> Path:
        """Return the index directory, defaulting to the domain directory."""
        if self.fst_index_dir:
            return Path(self.fst_index_dir).expanduser().resolve()
        return self.dir


@dataclass
class Config:
    """Top-level configuration."""

    domains: dict[str, DomainConfig] = field(default_factory=dict)
    history_file: Optional[Path] = None
    log_file: Optional[Path] = None


def _resolve_path(value: str) -> Path:
    """Expand ~ and resolve to an absolute path."""
    return Path(value).expanduser().resolve()


def load_config(path: Optional[Path] = None) -> Config:
    """Load configuration from a TOML file.

    Falls back to sensible defaults if no config file exists.
    """
    cfg = Config()

    # Determine config path
    config_path = path
    if config_path is None:
        env_path = os.environ.get("UNIFIED_HISTORY_CONFIG")
        if env_path:
            config_path = Path(env_path).expanduser()
        else:
            user_path = Path.home() / ".config" / "unified-history-mcp" / "config.toml"
            if user_path.exists():
                config_path = user_path
            else:
                cwd_path = Path.cwd() / "unified-history.toml"
                if cwd_path.exists():
                    config_path = cwd_path

    if config_path is None or not config_path.exists():
        return cfg

    raw = config_path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))

    # Parse [domains.X] sections
    domains_raw = data.get("domains", {})
    for name, d in domains_raw.items():
        domain_dir = _resolve_path(d.get("dir", "~"))
        ext_list: list[str] = d.get("extensions", [])
        if isinstance(ext_list, list):
            ext_list = [str(e) for e in ext_list]

        filters_list: list[str] = d.get("filters", [])
        if isinstance(filters_list, list):
            filters_list = [str(f) for f in filters_list]

        domain_cfg = DomainConfig(
            name=name,
            dir=domain_dir,
            pattern=str(d.get("pattern", "*")),
            type=str(d.get("type", "files")),
            extensions=ext_list,
            extractor=str(d.get("extractor", "jsonl")),
            renderer=str(d.get("renderer", "")),
            label=str(d.get("label", "file")),
            fst_binary=str(d.get("fst_binary", "fst-indexer")),
            fst_pattern=str(d.get("fst_pattern", "")),
            fst_index_dir=d.get("fst_index_dir"),
            filters=filters_list,
        )
        cfg.domains[name] = domain_cfg

    # Parse [history] section
    hist_raw = data.get("history")
    if hist_raw and "file" in hist_raw:
        cfg.history_file = _resolve_path(hist_raw["file"])

    # Parse [log] section
    log_raw = data.get("log")
    if log_raw and "file" in log_raw:
        cfg.log_file = _resolve_path(log_raw["file"])

    return cfg
