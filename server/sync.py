# OWNER: P3
"""sync.py: Attaches the board to every room and brings joining clients up to date.

Contract sections C3 and E4. Two room hooks (server/rooms.py):

    on_room_created : room.board = BoardState(), room.undo = UndoManager()
    on_member_joined: send SNAPSHOT (first join, or a gap that is too big) or
                      SYNC_DELTA (a member that came back with its token and
                      last_seq, and missed at most config.MAX_DELTA operations)

The hooks run after JOIN_OK was sent and in bootstrap order, so a joining
client receives USER_LIST, then this SNAPSHOT / SYNC_DELTA, then CHAT_HISTORY.
"""
from common import protocol
from server import rooms
from server.board_state import BoardState
from server.undo import UndoManager


@rooms.on_room_created
def attach_board(room):
    """Give a new room its (empty) board state and its undo/redo stacks."""
    room.board = BoardState()
    room.undo = UndoManager()


@rooms.on_member_joined
def send_board_state(room, member, rejoin, last_seq):
    """Send the joining member the current board.

    rejoin=True with a usable last_seq: send only the operations it missed
    (SYNC_DELTA). Anything else, or a gap above MAX_DELTA, or a last_seq the
    log cannot serve (BoardState.delta returns None): send the whole board
    (SNAPSHOT). The client replaces its board with a SNAPSHOT and sets its
    last_seq to the snapshot's seq.

    rooms.manager.member_joined already holds room.lock, and board_ops holds
    it for every operation, so the state read here is never half-way through
    an operation and no operation falls between the snapshot and the
    broadcasts that follow it.
    """
    with room.lock:
        board = room.board
        if rejoin and last_seq is not None:
            ops = board.delta(last_seq)
            if ops is not None:
                room.send_to(member.name, protocol.SYNC_DELTA,
                             {'from_seq': last_seq, 'to_seq': board.seq, 'ops': ops})
                return
        room.send_to(member.name, protocol.SNAPSHOT, board.snapshot_payload())
