"""bootstrap.py: Imports every server module so their @handler and rooms hooks register.

Contract section G. Plain static imports (PyInstaller cannot see dynamic
imports). The ORDER matters: hooks run in registration order, so a joining
client gets USER_LIST (presence), then SNAPSHOT/SYNC_DELTA (Janith), then
CHAT_HISTORY (chat stays last among the modules), then VERSIONS (the explicit
registration at the bottom of this file).
"""

# Sharwan
from server import dispatcher  # noqa: F401
from server import rooms  # noqa: F401
from server import presence  # noqa: F401
# Harshith
from server import permissions  # noqa: F401
# Janith
from server import board_state  # noqa: F401
from server import board_ops  # noqa: F401
from server import undo  # noqa: F401
from server import sync  # noqa: F401
from server import versions  # noqa: F401
from server import assets  # noqa: F401
# Harshith (last)
from server import chat  # noqa: F401

# The last join step, registered after chat's hook so VERSIONS follows
# CHAT_HISTORY: JOIN_OK, USER_LIST, SNAPSHOT, CHAT_HISTORY, VERSIONS.
rooms.on_member_joined(versions.send_versions_on_join)
