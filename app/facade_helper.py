"""Transparent backward-compatible module facade helper (H-1).

Allows legacy top-level module names (`start_with_env`, `mei_emotion_router`,
`console_server`, `patch_registry`, `pc_audio_router`, `reply_length_router`,
`nmc_weather_patch`) to delegate live attribute reads, writes, deletes, and
`unittest.mock.patch.object(...)` calls cleanly to their modular implementations
under `patches/` and `console/`.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, Iterable, Mapping, Optional


class DelegatingModule(types.ModuleType):
    """Module subclass forwarding reads/writes to owning submodules while keeping __dict__ in sync."""

    def __init__(
        self,
        name: str,
        doc: Optional[str],
        submodules: Iterable[types.ModuleType],
        own_dict: Mapping[str, Any],
        *,
        explicit_owners: Optional[Mapping[str, types.ModuleType]] = None,
    ) -> None:
        super().__init__(name, doc)
        subs = list(submodules)
        object.__setattr__(self, "_facade_submodules", subs)
        owners: Dict[str, types.ModuleType] = {}
        if explicit_owners:
            for k, sub in explicit_owners.items():
                owners[k] = sub
                if hasattr(sub, k):
                    self.__dict__[k] = getattr(sub, k)
        for sub in subs:
            for k, v in vars(sub).items():
                if k.startswith("__") and k.endswith("__"):
                    continue
                if k not in owners:
                    owners[k] = sub
                    self.__dict__[k] = v
        for k, v in own_dict.items():
            if k in ("_facade_owners", "_facade_submodules"):
                continue
            self.__dict__[k] = v
        object.__setattr__(self, "_facade_owners", owners)

    def __getattribute__(self, name: str) -> Any:
        if name in (
            "__dict__",
            "__class__",
            "_facade_owners",
            "_facade_submodules",
            "__name__",
            "__doc__",
            "__file__",
            "__loader__",
            "__package__",
            "__spec__",
            "__path__",
        ):
            return object.__getattribute__(self, name)
        owners = object.__getattribute__(self, "_facade_owners")
        owner = owners.get(name)
        if owner is not None:
            return getattr(owner, name)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        owners = object.__getattribute__(self, "_facade_owners")
        owner = owners.get(name)
        if owner is not None:
            setattr(owner, name, value)
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        owners = object.__getattribute__(self, "_facade_owners")
        owner = owners.get(name)
        if owner is not None and hasattr(owner, name):
            delattr(owner, name)
        if name in object.__getattribute__(self, "__dict__"):
            object.__delattr__(self, name)

    def __dir__(self) -> list[str]:
        owners = object.__getattribute__(self, "_facade_owners")
        return sorted(set(object.__getattribute__(self, "__dict__").keys()) | set(owners.keys()))


def install_facade(
    module_name: str,
    submodules: Iterable[types.ModuleType],
    *,
    explicit_owners: Optional[Mapping[str, types.ModuleType]] = None,
) -> DelegatingModule:
    """Replace `sys.modules[module_name]` with a DelegatingModule forwarding to `submodules`."""
    orig = sys.modules[module_name]
    preserve = {
        k: v
        for k, v in vars(orig).items()
        if k in ("__file__", "__loader__", "__package__", "__spec__", "__cached__", "__builtins__")
    }
    facade = DelegatingModule(
        orig.__name__,
        orig.__doc__,
        submodules,
        preserve,
        explicit_owners=explicit_owners,
    )
    sys.modules[module_name] = facade
    return facade
