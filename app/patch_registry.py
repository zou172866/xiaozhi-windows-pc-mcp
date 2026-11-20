"""Backward-compatible facade for `patches.registry` (H-1)."""

from facade_helper import install_facade
from patches import registry as _impl

install_facade(__name__, [_impl])
