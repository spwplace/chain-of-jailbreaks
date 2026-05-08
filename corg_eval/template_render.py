from __future__ import annotations

from pathlib import Path
from typing import Any

from .tactics import TEMPLATE_IDS


def render_lure_template(template_id: str, context: dict[str, Any]) -> str:
    if template_id not in TEMPLATE_IDS:
        raise ValueError(f"Unknown template {template_id!r}; choose one of {', '.join(TEMPLATE_IDS)}")
    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
    except ImportError as exc:
        raise RuntimeError("Install generator extras with `pip install -e '.[generator]'`.") from exc

    template_dir = Path(__file__).with_name("templates")
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    return env.get_template(f"{template_id}.j2").render(**context).strip()
