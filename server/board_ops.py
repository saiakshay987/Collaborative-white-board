# OWNER: P3
"""board_ops.py: Server handlers for OP_ADD, OP_STROKE_PART, OP_UPDATE, OP_DELETE and OP_CLEAR.

Contract sections C2 and E. Every board operation goes through the same
pipeline, in this order:

    1. permissions.can(role, 'draw')  (OP_CLEAR: 'clear')     -> else ERROR FORBIDDEN
    2. check the payload; new objects go through models.validate_obj -> else ERROR BAD_FORMAT
    3. with room.lock (one operation at a time per room, so the seq order is the truth):
         - set seq (board.seq + 1), ts, owner and z (owner and z are set inside BoardState.apply)
         - apply it to the BoardState (this also appends it to the log)
         - undo.record(...) so the sender can undo it
         - room.broadcast to EVERY online member, the sender included (echo rule),
           keeping the client's op_id so the sender can match the echo to its pending op
    4. a rejection (any BoardError) is answered with ERROR to the SENDER ONLY and
       is never broadcast; it changes no state and uses no seq.

Never trust the client: user, seq and ts come from the server (the dispatcher
already overwrote them in `msg`); owner and z are written by BoardState; the
payload that is applied and broadcast is rebuilt from the fields the contract
defines, so unknown extra fields never reach the other clients.
"""
import copy
import math
import numbers
import time

from common import models, protocol
from server import permissions
from server.board_state import (BoardError, PROTECTED_FIELDS, STROKE_DEFAULTS,
                                MAX_PART_POINTS, MAX_POINTS)
from server.dispatcher import handler


# ---------------------------------------------------------------- payload checks
# Each clean_* function takes the raw client payload and returns the payload
# that will be applied and broadcast, or raises BoardError. They run BEFORE
# the lock is taken and never touch the board.

def _is_number(value):
    """True for a finite int or float. bool is excluded (True is an int in Python)."""
    return (isinstance(value, numbers.Real) and not isinstance(value, bool)
            and math.isfinite(value))


def _need_obj_id(payload):
    """Return payload['obj_id'] or raise BAD_FORMAT if it is not a non-empty string."""
    oid = payload.get('obj_id')
    if not isinstance(oid, str) or not oid:
        raise BoardError(protocol.BAD_FORMAT, 'obj_id must be a non-empty string')
    return oid


def _validated(obj):
    """Raise BAD_FORMAT unless obj passes models.validate_obj (contract section D)."""
    ok, reason = models.validate_obj(obj)
    if not ok:
        raise BoardError(protocol.BAD_FORMAT, 'invalid object: ' + reason)


def clean_add(payload):
    """OP_ADD {obj}: validate the complete object and fill in the kind's defaults."""
    obj = payload.get('obj')
    _validated(obj)
    obj = copy.deepcopy(obj)
    # Fields a kind lists under "defaults" may be left out by the client;
    # fill them in so every client stores the same complete object.
    for field, value in models.KINDS[obj['kind']].get('defaults', {}).items():
        obj.setdefault(field, copy.deepcopy(value))
    return {'obj': obj}


def clean_stroke_part(payload):
    """OP_STROKE_PART {obj_id, points, done, style?}: check the points and the style.

    The first part of a stroke carries the style and creates the object, so
    that stroke is validated like an OP_ADD would be. The style is rebuilt
    with exactly the four fields BoardState stores, defaults filled in.
    """
    oid = _need_obj_id(payload)
    points = payload.get('points')
    if not isinstance(points, list):
        raise BoardError(protocol.BAD_FORMAT, 'points must be a list')
    if len(points) > MAX_PART_POINTS:
        raise BoardError(protocol.TOO_LARGE, 'more than %d points in one part' % MAX_PART_POINTS)
    for pt in points:
        if not (isinstance(pt, list) and len(pt) == 2 and _is_number(pt[0]) and _is_number(pt[1])):
            raise BoardError(protocol.BAD_FORMAT, 'every point must be [x, y]')
    clean = {'obj_id': oid, 'points': copy.deepcopy(points), 'done': payload.get('done') is True}
    style = payload.get('style')
    if style is not None:
        if not isinstance(style, dict):
            raise BoardError(protocol.BAD_FORMAT, 'style must be an object')
        clean['style'] = {key: copy.deepcopy(style.get(key, default))
                          for key, default in STROKE_DEFAULTS.items()}
        _validated(dict(clean['style'], id=oid, kind='stroke', points=clean['points']))
    return clean


def clean_update(payload):
    """OP_UPDATE {obj_id, set}: id, kind, owner and z can never be changed."""
    oid = _need_obj_id(payload)
    fields = payload.get('set')
    if not isinstance(fields, dict) or not fields:
        raise BoardError(protocol.BAD_FORMAT, 'set must be a non-empty object')
    bad = PROTECTED_FIELDS.intersection(fields)
    if bad:
        raise BoardError(protocol.FORBIDDEN, 'cannot update %s' % sorted(bad))
    if isinstance(fields.get('points'), list) and len(fields['points']) > MAX_POINTS:
        raise BoardError(protocol.TOO_LARGE, 'more than %d points' % MAX_POINTS)
    return {'obj_id': oid, 'set': copy.deepcopy(fields)}


def clean_delete(payload):
    """OP_DELETE {obj_id}."""
    return {'obj_id': _need_obj_id(payload)}


def clean_clear(payload):
    """OP_CLEAR {}: no fields."""
    return {}


def check_update(board, payload):
    """Under the lock: the object must still be valid after the update.

    The updated fields are merged into a copy of the stored object and the
    copy goes through models.validate_obj, so a client cannot turn a rect's
    width into a string and crash every renderer. A missing object is left
    to BoardState.apply, which answers NOT_FOUND.
    """
    obj = board.objects.get(payload['obj_id'])
    if obj is not None:
        merged = dict(obj)
        merged.update(payload['set'])
        _validated(merged)


# ---------------------------------------------------------------- the pipeline

def _object_id(payload):
    """The id of the object an operation touches (None for OP_CLEAR)."""
    if 'obj_id' in payload:
        return payload['obj_id']
    return (payload.get('obj') or {}).get('id')


def _process(session, msg, action, clean, check=None):
    """Run one board operation through the pipeline described in the module docstring.

    action: the permission to test ('draw' or 'clear'); clean: payload checker;
    check: optional extra check done under the lock (check(board, payload)).
    """
    room, user, op_type = session.room, msg['user'], msg['type']
    try:
        # 1. permission, with the role the SERVER stored for this member
        if not permissions.can(session.member.role, action):
            raise BoardError(protocol.FORBIDDEN, '%s is not allowed to %s' % (session.member.role, action))
        # 2. shape and content
        op_id = msg.get('op_id')
        if op_id is not None and not isinstance(op_id, str):
            raise BoardError(protocol.BAD_FORMAT, 'op_id must be a string')
        payload = clean(msg['payload'])

        # 3. one operation at a time per room: seq order == processing order
        with room.lock:
            board = room.board
            if check is not None:
                check(board, payload)
            env = protocol.make_msg(op_type, payload, room=room.code, user=user,
                                    op_id=op_id, seq=board.seq + 1, ts=time.time())
            prev = board.get_copy(_object_id(payload))   # state BEFORE, for undo
            env = board.apply(env)                       # sets owner and z, appends to the log
            room.undo.record(user, env, prev)
            # Broadcasting INSIDE the lock keeps the order on the wire equal to
            # the seq order: two threads can never interleave their broadcasts.
            # No `exclude`: the sender gets the echo too, which confirms its
            # optimistic local apply (contract C2 echo rule).
            room.broadcast(op_type, env['payload'], op_id=op_id, seq=env['seq'], user=user)
    except BoardError as exc:
        # 4. rejected: the sender only, nothing broadcast, nothing changed
        session.send_error(exc.code, exc.text, op_type)


# ---------------------------------------------------------------- handlers

@handler(protocol.OP_ADD)
def on_op_add(session, msg):
    """OP_ADD {obj}: add a complete object (shape, text, sticky, image)."""
    _process(session, msg, 'draw', clean_add)


@handler(protocol.OP_STROKE_PART)
def on_op_stroke_part(session, msg):
    """OP_STROKE_PART: create a stroke (first part, with style) or append points to it."""
    _process(session, msg, 'draw', clean_stroke_part)


@handler(protocol.OP_UPDATE)
def on_op_update(session, msg):
    """OP_UPDATE {obj_id, set}: move, resize or edit an object."""
    _process(session, msg, 'draw', clean_update, check_update)


@handler(protocol.OP_DELETE)
def on_op_delete(session, msg):
    """OP_DELETE {obj_id}: object eraser."""
    _process(session, msg, 'draw', clean_delete)


@handler(protocol.OP_CLEAR)
def on_op_clear(session, msg):
    """OP_CLEAR {}: remove every object. Host only (permission 'clear')."""
    _process(session, msg, 'clear', clean_clear)
