"""Backward-compatible facade for `patches.pc_audio` (H-1)."""

from facade_helper import install_facade
from patches import pc_audio as _impl

install_facade(__name__, [_impl])
