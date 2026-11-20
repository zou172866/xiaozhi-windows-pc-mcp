"""Backward-compatible facade for `patches.emotion` (H-1).

Delegates all attribute reads, writes, and unittest.mock.patch.object calls
directly to the modular submodules under `patches/emotion/`.
"""

from facade_helper import install_facade
from patches.emotion import farewell, prompt, splitter, tags, tts_route, wakeup

install_facade(__name__, [tags, splitter, prompt, tts_route, wakeup, farewell])
