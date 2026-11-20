"""Side-effect-free backward-compatible facade for `boot` and `patches/{auth,memory_hooks,asr}` (H-1).

Importing `start_with_env` performs ZERO side effects (no `os.chdir`, no threads, no patching).
Call `install_all()` (or run as `__main__`) to execute the full boot sequence.
"""

from __future__ import annotations

import os
import sys

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import boot as _boot
from facade_helper import install_facade
from patches import asr as _asr
from patches import auth as _auth
from patches import memory_hooks as _memory_hooks

if __name__ == "__main__":
    _boot.main()
else:
    install_facade(__name__, [_boot, _auth, _memory_hooks, _asr])
