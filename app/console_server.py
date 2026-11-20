"""Backward-compatible facade for `console` package (H-1).

Delegates all attribute reads, writes, and unittest.mock.patch.object calls
directly to `console/{shared,server}` and `console/routes/{gsv,logs,anima,config,l3,state}`.
"""

from facade_helper import install_facade
from console import server as _server
from console import shared as _shared
from console.routes import (
    anima as _anima,
    config as _config,
    gsv as _gsv,
    l3 as _l3,
    logs as _logs,
    state as _state,
)

install_facade(__name__, [_shared, _gsv, _logs, _l3, _anima, _config, _state, _server])
