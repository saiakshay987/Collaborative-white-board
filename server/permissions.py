# OWNER: P4
"""permissions.py: can(role, action): the role/permission table for host, editor and viewer.

Skeleton stub written by P1 so the server runs; P4 replaces can() with the
real table from contract section C4.
"""

# The actions of contract section F (server).
ACTIONS = frozenset([
    "draw", "undo", "upload", "version_save", "chat", "cursor",
    "clear", "version_restore", "set_role", "kick",
])


def can(role, action):
    """Return True if role may perform action.

    STUB: allows everything for now. P4 implements the real table.
    """
    return True
