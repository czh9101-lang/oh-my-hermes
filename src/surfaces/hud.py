from __future__ import annotations

from typing import Any

from ..version import __version__
from ..paths import OmhPaths
from ..plugin_bundle.omh.runtime_reader import read_omh_hud


def build_hud_payload(
    paths: OmhPaths,
    *,
    preset: str = "focused",
    limit: int = 3,
    token_metadata: dict[str, Any] | None = None,
    graph_preference: str = "auto",
) -> dict[str, Any]:
    return read_omh_hud(
        omh_home=paths.omh_home,
        hermes_home=paths.hermes_home,
        preset=preset,
        limit=limit,
        token_metadata=token_metadata or {},
        package_version=__version__,
        graph_preference=graph_preference,
    )
