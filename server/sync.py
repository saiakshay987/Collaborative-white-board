# OWNER: P3
"""sync.py: Attaches the board to every room and brings joining clients up to date.

Contract sections C3 and E4. Two room hooks (server/rooms.py):

    on_room_created : room.board = BoardState(), room.undo = UndoManager()
    on_member_joined: send SNAPSHOT (first join, or a gap that is too big) or
                      SYNC_DELTA (a member that came back with its token and
                      last_seq, and missed at most config.MAX_DELTA operations)
    @handler SYNC_REQUEST: the same choice, asked for by a client that saw a
                      gap in the seq numbers or a missing echo (contract E2/E3)

The hooks run after JOIN_OK was sent and in bootstrap order, so a joining
client receives USER_LIST, then this SNAPSHOT / SYNC_DELTA, then CHAT_HISTORY.
"""
from common import protocol
from server import rooms
from server.board_state import BoardState
from server.dispatcher import handler
from server.undo import UndoManager


@rooms.on_room_created
def attach_board(room):
    """Give a new room its (empty) board state and its undo/redo stacks."""
    room.board = BoardState()
    room.undo = UndoManager()


def _send_state(room, name, last_seq):
    """Send member `name` what it needs to reach the current board.

    last_seq is the client's last applied seq (None when it has nothing).
    If BoardState.delta() can serve it (gap at most MAX_DELTA and still in
    the log) the answer is SYNC_DELTA {from_seq, to_seq, ops}; otherwise,
    or when last_seq is None or nonsense, it is a full SNAPSHOT. The client
    replaces its board with a SNAPSHOT and sets its last_seq to the
    snapshot's seq.

    The caller holds room.lock, and board_ops holds it for every operation,
    so the state read here is never half-way through an operation and no
    operation falls between this answer and the broadcasts that follow it.
    """
    board = room.board
    ops = board.delta(last_seq) if last_seq is not None else None
    if ops is not None:
        room.send_to(name, protocol.SYNC_DELTA,
                     {'from_seq': last_seq, 'to_seq': board.seq, 'ops': ops})
    else:
        room.send_to(name, protocol.SNAPSHOT, board.snapshot_payload())


@rooms.on_member_joined
def send_board_state(room, member, rejoin, last_seq):
    """Join hook: SNAPSHOT on a first join, SYNC_DELTA (or SNAPSHOT) on a rejoin.

    A first join has nothing to continue from, so last_seq is ignored unless
    rejoin is True. rooms.manager.member_joined already holds room.lock.
    """
    with room.lock:
        _send_state(room, member.name, last_seq if rejoin else None)


@handler(protocol.SYNC_REQUEST)
def on_sync_request(session, msg):
    """SYNC_REQUEST {last_seq}: the client noticed a gap or a missing echo (contract E2, E3).

    Answered to the sender only with SYNC_DELTA when board.delta() works,
    otherwise with a SNAPSHOT. Every member may ask, viewers included: it
    changes nothing on the board. A missing or non-integer last_seq simply
    gets a SNAPSHOT (delta() returns None for it).
    """
    with session.room.lock:
        _send_state(session.room, session.member.name, msg['payload'].get('last_seq'))
