# -*- mode: python ; coding: utf-8 -*-
"""WhiteboardClient-console.spec: the client WITH a console window, for debugging.

Identical to WhiteboardClient.spec (it runs that file), except that the
executable keeps a console window where print() output and Python tracebacks
appear, and it is named WhiteboardClient-console. Give it to someone whose
client misbehaves and ask for a screenshot of the console.
"""

import os

CONSOLE = True
_spec = os.path.join(SPECPATH, "WhiteboardClient.spec")
with open(_spec, "r", encoding="utf-8") as _f:
    exec(compile(_f.read(), _spec, "exec"))
