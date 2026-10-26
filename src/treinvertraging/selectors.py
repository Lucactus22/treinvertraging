"""Site-specific locator/label/endpoint config, kept out of code so it can be fixed without a rebuild.

Each site has its own YAML file: ``ns.yaml``, ``ovchip.yaml``, ``gtbv.yaml``.
Lookup order per file: ``CONFIG_DIR/selectors/<site>.yaml``, then the copy bundled with the
package (``treinvertraging/defaults/selectors/<site>.yaml``). A user file is deep-merged over the
bundled defaults, so it only needs to contain the keys that differ.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from treinvertraging.config import get_settings

DEFAULTS_DIR = Path(__file__).parent / "defaults" / "selectors"

_cache: dict[tuple[str, float, float], dict[str, Any]] = {}


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_selectors(site: str) -> dict[str, Any]:
    """Return the merged selector config for ``site`` (cached until either file changes)."""
    default_path = DEFAULTS_DIR / f"{site}.yaml"
    user_path = get_settings().CONFIG_DIR / "selectors" / f"{site}.yaml"
    key = (str(user_path), _mtime(default_path), _mtime(user_path))
    if key not in _cache:
        _cache[key] = _merge(_read(default_path), _read(user_path))
    return _cache[key]


def get(site: str, dotted: str, default: Any = None) -> Any:
    """``get("gtbv", "start.next_button")`` -> value at that dotted path, or ``default``."""
    node: Any = load_selectors(site)
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def reset_cache() -> None:
    _cache.clear()
