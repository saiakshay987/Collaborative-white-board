"""permissions.py: can(role, action): the role/permission table for host, editor and viewer.

Implements contract section C4. The server calls can(member.role, action)
BEFORE it changes any room state; the client is never trusted to enforce roles
(the client only greys out buttons for convenience).
"""

# The actions of contract section F (server).
ACTIONS = frozenset([
    "draw", "undo", "upload", "version_save", "chat", "cursor",
    "clear", "version_restore", "version_delete", "version_rename", "set_role", "kick",
])

# Actions every non-viewer may do: everything that changes the board.
_EDITOR_ACTIONS = frozenset(["draw", "undo", "upload", "version_save", "chat", "cursor"])

# role -> set of allowed actions (contract C4).
#   host   : everything, including the destructive/administrative actions
#            (clear, version_restore, version_delete, version_rename, set_role, kick)
#   editor : draw, undo, upload, version_save, chat, cursor
#   viewer : read-only, may only chat and move a cursor
_TABLE = {
    "host": ACTIONS,
    "editor": _EDITOR_ACTIONS,
    "viewer": frozenset(["chat", "cursor"]),
}


def can(role, action):
    """Return True if `role` may perform `action`, according to contract C4.

    Default deny: an unknown role, an unknown action, or a value of the wrong
    type (e.g. None or a list coming from a malformed message) returns False.
    """
    try:
        return action in _TABLE.get(role, frozenset())
    except TypeError:  # unhashable role (e.g. a list from a hostile client)
        return False
