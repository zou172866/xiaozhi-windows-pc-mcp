"""Backward-compatible facade for `patches.weather` (H-1)."""

from facade_helper import install_facade
from patches import weather as _impl

install_facade(__name__, [_impl])
