"""ops.py: send_op, the single path for every board change (contract section F, frozen).

Every tool (Akshay, Janith, Harshith) changes the board ONLY through send_op; nobody sends
OP_* with net.send directly. send_op implements optimistic apply (contract
E3): the change is applied locally at once, remembered as pending by op_id,
and the server's echo (same op_id, now with seq) later confirms it.
"""

from common import protocol


def send_op(app, type, payload):
    """Apply a board operation locally, send it to the server, return its op_id.

    type must be a board operation (OP_*, UNDO, REDO). The op_id
    "<user>-<counter>" lets BoardModel match the server echo and drop
    duplicates.
    """
    if type not in protocol.BOARD_OPS:
        raise ValueError("send_op is only for board operations, not %r" % (type,))
    op_id = protocol.new_op_id(app.me.username or "me")
    if app.board is not None:
        app.board.apply_local(type, payload, op_id)
    if app.net is not None:
        app.net.send(type, payload, op_id=op_id)
    return op_id
