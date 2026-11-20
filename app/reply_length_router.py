"""Backward-compatible facade for `patches.reply_length` (H-1)."""

from facade_helper import install_facade
from patches import reply_length as _impl

install_facade(__name__, [_impl])
