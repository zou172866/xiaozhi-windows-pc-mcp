"""Modular monkey-patch package for xiaozhi-esp32-server (H-1).

Importing `patches` or any submodule performs ZERO runtime side effects:
no `os.chdir`, no background threads, and no monkey-patching until `boot.install_all()`
is explicitly invoked.
"""

from . import registry

__all__ = ["registry"]
