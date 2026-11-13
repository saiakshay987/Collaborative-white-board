"""config.py: Shared constants used by both the server and the client.

Every value here is fixed by docs/CONTRACT.md (sections A, B, D and E).
Change a value only through a contract revision, because both ends of the
connection must agree on it.
"""

# --- Transport (contract section A) ------------------------------------------

# TCP port the server listens on (it binds 0.0.0.0:PORT).
PORT = 5050

# Largest allowed frame body in bytes (8 MiB). A frame header announcing more
# than this is a protocol error and the connection is closed. This stops a
# broken or malicious peer from making us allocate gigabytes of memory.
MAX_FRAME = 8 * 1024 * 1024

# --- Liveness and recovery (contract sections C1 and E) ----------------------

# The server sends PING to every client this often (seconds).
HEARTBEAT_S = 5

# No frame at all from a client for this long (seconds) marks it offline.
TIMEOUT_S = 15

# An offline member keeps its seat (name, role, color, token) this long
# (seconds); reconnecting inside the window resumes with SYNC_DELTA/SNAPSHOT.
REJOIN_WINDOW_S = 120

# Largest seq gap the server repairs with SYNC_DELTA; a bigger gap gets a
# full SNAPSHOT instead.
MAX_DELTA = 500

# --- Board geometry (contract section D) -------------------------------------

# Board size in board units, origin at the top-left corner.
BOARD_W = 4000
BOARD_H = 3000

# Snap-to-grid step in board units (snapping is done on the client).
GRID = 20

# --- Users (contract section B) ----------------------------------------------

# Eight fixed, clearly distinct colors, handed out in join order.
USER_COLORS = [
    "#e6194b",  # red
    "#3cb44b",  # green
    "#4363d8",  # blue
    "#f58231",  # orange
    "#911eb4",  # purple
    "#008080",  # teal
    "#f032e6",  # magenta
    "#9a6324",  # brown
]
