# CONTRACT.md

Revision 6. Frozen: additions only, applied by P1.

Revision 6: version history can be managed. Two new message types, VERSION_DELETE and VERSION_RENAME (C3), both host only, and two new permission actions, version_delete and version_rename (C4).

Revision 5: the room list. Two new message types, ROOM_LIST_REQUEST and ROOM_LIST (C1), the only request a connection may send before it has joined besides CREATE_ROOM, JOIN_ROOM and PONG; `autosave.json` now holds the whole room (E7), so a restarted server lists and re-opens every room with its password, host, colors and roles (E8).

Revision 4 (no new message types or fields): rooms are kept while the server runs (E8), the join order ends with VERSIONS (C1), `app.reset_session()` and `@on_session_reset` (F, G).

Contract changes: post in the group chat as `CONTRACT CHANGE: <what, why>`. P1 applies it within 30 minutes and replies `CONTRACT rev N`. Until then, keep using the old shape.

## A. Transport and framing

- TCP, one persistent connection per client. The server binds 0.0.0.0 on port 5050 (`config.PORT`).
- A frame is a 4-byte big-endian unsigned length N followed by N bytes of UTF-8 JSON. `config.MAX_FRAME` is 8 MiB; a larger N is a protocol error and the connection is closed.
- `framing.send_msg(sock, msg)` holds a per-socket lock. `framing.recv_msg(sock)` loops until exactly N bytes have arrived (TCP is a byte stream) and returns None when the peer closed.
- Both update `framing.STATS` (msgs_in, msgs_out, bytes_in, bytes_out) for the traffic panel.
- Why TCP: a reliable, ordered stream. The application adds sequence numbers only to repair gaps caused by dropped connections. UDP is used only by the optional LAN discovery.

## B. Envelope

```json
{"v":1, "type":"OP_ADD", "room":"K7QX2M", "user":"alice", "op_id":"alice-41",
 "seq":null, "ts":0.0, "payload":{}}
```

- `v`: always 1.
- `type`: one of section C.
- `room`: 6-character room code (A-Z and 2-9, no 0, O, 1, I).
- `user`: sender name. The SERVER overwrites it with the authenticated name; clients never set it.
- `op_id`: set by the CLIENT on every message that changes the board (OP_*, UNDO, REDO) with `protocol.new_op_id(user)`, which returns `<user>-<counter>`. Used for dedupe and to match the server echo. Null otherwise.
- `seq`: set ONLY by the server, on accepted board operations. One integer counter per room, starts at 1, +1 per accepted operation. Null otherwise.
- `ts`: server time in epoch seconds (float), set on every message the server sends.
- Errors: the server replies `ERROR {code, text, ref_type}` to the sender only. Codes: BAD_FORMAT, NO_ROOM, BAD_PASSWORD, NAME_TAKEN, FORBIDDEN, TOO_LARGE, NOT_FOUND.
- Users get a color from `config.USER_COLORS` (8 fixed colors) in join order. Passwords are stored as salted sha256 (never in clear text); the hash and salt are also written to the room's autosave file (E7), so a locked room stays locked after a server restart.

## C. Messages

### C1. Session and presence (owner P1)

| Type | Direction | Payload | Notes |
|---|---|---|---|
| CREATE_ROOM | C to S | {username, password?} | Sender becomes host. Reply: JOIN_OK |
| JOIN_ROOM | C to S | {room, username, password?, token?, last_seq?} | token and last_seq only on reconnect |
| JOIN_OK | S to C | {room, token, role, color, users} | Then the server runs the join hooks: USER_LIST, SNAPSHOT or SYNC_DELTA (P3), CHAT_HISTORY (P4), VERSIONS (P3). The order is fixed |
| LEAVE | C to S | {} | Clean exit |
| USER_LIST | S to C | {users:[{name, color, role, online}]} | Sent to everyone on every change, role changes included |
| USER_JOINED, USER_LEFT | S to C | {name, color, reason?} | reason: leave, timeout or kicked |
| PING | S to C | {t} | Every 5 s (`config.HEARTBEAT_S`) |
| PONG | C to S | {t} | Echoes t; the server computes RTT |
| STATS | S to C | {rtt:{name:ms}, server:{clients, msgs_in, msgs_out, bytes_in, bytes_out}} | Every 2 s |
| CURSOR | C to S, relayed to others | {x, y} in board units | Max 20 per second per client. Not logged, no seq |
| KICK | C to S | {user} | Host only |
| ROOM_LIST_REQUEST | C to S | {} | Allowed BEFORE joining a room (the join screen). Changes nothing; the reply goes to the sender only |
| ROOM_LIST | S to C | {rooms:[{code, host, online, members, locked, objects, updated}]} | Reply to ROOM_LIST_REQUEST, newest `updated` first. host: username of the host or null; online: members connected now; members: every username the room remembers; locked: has a password; objects: objects on the board; updated: epoch seconds of the last change. No member names, chat or board content are included |
| ERROR | S to C | {code, text, ref_type} | See section B |

### C2. Board operations (server side owner P3; sent by tools of P2, P3, P4 through client/ops.py send_op)

| Type | Payload | Notes |
|---|---|---|
| OP_ADD | {obj} | A complete object (shape, text, sticky, image). Server sets owner and z |
| OP_STROKE_PART | {obj_id, points:[[x,y],...], done, style?} | The first part creates the stroke and carries style {tool, color, width, alpha}; later parts append points; done:true on the last. At most 50 points per part, flushed every 40 ms |
| OP_UPDATE | {obj_id, set:{field:value}} | Move, resize, edit text |
| OP_DELETE | {obj_id} | Object eraser: removes the whole object |
| OP_CLEAR | {} | Host only |
| UNDO, REDO | {} | The server answers with ordinary OP_* messages (inverse operations) with fresh seq, so clients need no undo logic |

Echo rule: every accepted operation is broadcast to ALL clients of the room, INCLUDING the sender, with seq, ts and the original op_id. A rejected operation gets an ERROR to the sender only and is never broadcast.

### C3. State, history and images (owner P3)

| Type | Direction | Payload | Notes |
|---|---|---|---|
| SNAPSHOT | S to C | {seq, enc:'zlib-b64', data} | data is base64(zlib(JSON {objects:[...]})). The client replaces its whole board and sets last_seq to seq. Sent on join, on a failed delta, after VERSION_RESTORE |
| SYNC_REQUEST | C to S | {last_seq} | Answered with SYNC_DELTA or SNAPSHOT |
| SYNC_DELTA | S to C | {from_seq, to_seq, ops:[envelopes]} | Used when the gap is at most `config.MAX_DELTA` (500) |
| VERSION_SAVE | C to S | {name} | Host or editor |
| VERSION_LIST | C to S | {} | |
| VERSIONS | S to C | {versions:[{id, name, by, ts, seq, count}]} | Pushed to all on any change and on VERSION_LIST |
| VERSION_RESTORE | C to S | {id} | Host only; followed by SNAPSHOT to all |
| VERSION_DELETE | C to S | {id} | Host only. Removes the version from memory and its file from disk. Unknown id: ERROR NOT_FOUND. Then VERSIONS to all |
| VERSION_RENAME | C to S | {id, name} | Host only. name is trimmed and must be 1 to 60 characters (else ERROR BAD_FORMAT). Unknown id: ERROR NOT_FOUND. Then VERSIONS to all |
| IMG_BEGIN | both | {asset_id, name, mime, size, chunks, w, h} | Upload (C to S) or download (S to C) |
| IMG_CHUNK | both | {asset_id, idx, data} | data is base64 of at most 24 KB of raw bytes |
| IMG_END | both | {asset_id, sha256} | Receiver verifies size and hash |
| IMG_OK | S to C | {asset_id} | Upload accepted; the client then sends OP_ADD with an image object |
| ASSET_GET | C to S | {asset_id} | Server streams IMG_BEGIN, IMG_CHUNK x n, IMG_END back |

Image limits: 5 MB per image; the client downscales to 1600 px on the longest side before upload.

### C4. Chat and access (owner P4)

| Type | Direction | Payload | Notes |
|---|---|---|---|
| CHAT | C to S | {text} | At most 1000 characters |
| CHAT | S to C | {msg_id, from, text, ts} | Broadcast to the room, sender included |
| DM | C to S | {to, text} | |
| DM | S to C | {msg_id, from, to, text, ts} | Delivered ONLY to the recipient and echoed to the sender |
| CHAT_HISTORY | S to C | {messages:[{msg_id, type, from, to?, text, ts}]} | Last 200 room messages plus this user's own DMs, sent after join |
| SET_ROLE | C to S | {user, role} | Host only. The server then sends USER_LIST to all |

Roles and permissions (`permissions.can(role, action)`; the creator is host, joiners default to editor):

| Action | Allowed roles |
|---|---|
| draw (OP_ADD, OP_STROKE_PART, OP_UPDATE, OP_DELETE), undo (UNDO, REDO), upload (IMG_*), version_save | host, editor |
| chat (CHAT, DM), cursor | host, editor, viewer |
| clear, version_restore, version_delete (VERSION_DELETE), version_rename (VERSION_RENAME), set_role, kick | host |

## D. Board objects

- The board is 4000 x 3000 units (`config.BOARD_W`, `config.BOARD_H`), origin top-left.
- Clients zoom and pan locally; every coordinate in a message is in board units with at most one decimal.
- Grid size is 20 units (`config.GRID`). Snap-to-grid happens on the CLIENT before sending, so the server stores snapped values.
- Every object has: id (obj_id, `<user>-<counter>`, made by the client), kind, owner (set by the server) and z (set by the server: seq at creation, which is the paint order).
- Limits: 20000 objects per room, 20000 points per stroke.

| kind | Fields | Validator file |
|---|---|---|
| stroke | points [[x,y],...], color '#rrggbb', width 1-40, tool (pen, marker or highlighter), alpha 0-1 | common/kinds_draw.py (P2) |
| line, arrow | x1, y1, x2, y2, color, width | common/kinds_draw.py (P2) |
| rect, ellipse | x, y, w, h, color, width, fill (null or '#rrggbb') | common/kinds_draw.py (P2) |
| text | x, y, w, h, text, color, size 8-96, font, bold, italic, underline, align | common/kinds_text.py (P4) |
| sticky | x, y, w, h, text, color (background), size 8-96, font, bold, italic, underline, align, text_color | common/kinds_text.py (P4) |
| image | x, y, w, h, asset_id | common/kinds_image.py (P3) |

Each `kinds_*.py` exports `KINDS = {kind: {'required': {field: type}, 'defaults': {...}}}`. `models.validate_obj(obj)` returns `(ok, reason)` from the merged registry.

Text format fields (Revision 2, all optional, the server fills the defaults in so objects saved without them still load): `font` (family name, 1-64 characters, default "Segoe UI"), `size` (board units, text 18 / sticky 16), `bold`, `italic`, `underline` (booleans, default false), `align` ("left", "center" or "right", default "left"), and for a sticky `text_color` ('#rrggbb', default '#2b2b2b'; its `color` stays the background). Text is drawn in board units under the canvas transform, so it scales with the box when zooming and in exports. A format change is an ordinary `OP_UPDATE {obj_id, set:{field: value}}`; no new message type.

Each KINDS entry may also carry an optional `"limits": {field: [min, max]}`. For numbers the value must be within [min, max]; for strings the LENGTH must be. `validate_obj` rejects an object outside its limits with a reason naming the field. The server validates on OP_ADD and on OP_UPDATE (the object after applying `set`), replying ERROR BAD_FORMAT.

## E. Ordering, duplicates, concurrency and recovery

1. Server ordering. Board operations are processed one at a time per room under `room.lock`. The seq order is the truth. Two edits to the same object: the higher seq wins, field by field.
2. Client ordering (BoardModel, P3). Keep `last_seq`. seq equals last_seq + 1: apply. seq at most last_seq, or op_id already applied: drop as a duplicate. seq above last_seq + 1: buffer it; if the gap is not filled within 1 s, send SYNC_REQUEST {last_seq} (at most once every 2 s).
3. Optimistic apply. A local operation is applied at once and remembered as pending by op_id. The server echo confirms it without drawing it twice. No echo within 5 s: SYNC_REQUEST.
4. Reconnect. NetClient retries every 2 s for up to 60 s and sends JOIN_ROOM with token and last_seq. The server keeps an offline member for 120 s (`config.REJOIN_WINDOW_S`) and answers with SYNC_DELTA, or SNAPSHOT when the gap is above MAX_DELTA. After 120 s the user joins as a new member.
5. Disconnect detection. No frame from a client for 15 s (`config.TIMEOUT_S`) marks it offline and sends USER_LEFT with reason timeout.
6. Undo and redo. UNDO reverts only the sender's own most recent undoable action (add or stroke, update, delete). OP_CLEAR is not undoable, but the server takes an automatic version first. A new action clears the sender's redo stack. If the target object no longer exists the server moves to the previous entry; with nothing left it replies ERROR NOT_FOUND.
7. Autosave. Every 10 s the server writes `server_data/<room>/autosave.json` for every room that changed: board and seq, versions metadata, assets index, chat log, password hash and salt, host username and every member's color and role. A restarted server loads every saved room, with nobody online, and lists it in ROOM_LIST. Files from an earlier revision (without the people and chat fields) still load as open rooms.
8. Rooms outlive their members. A room is never deleted while the server runs; when the last member leaves it keeps board, chat, versions and assets. The server remembers each username's color and role (`room.profiles`) and the room's host username (`room.host_name`): a user who joins again with the same name gets the same color and role, and the original host gets host back when nobody else holds it. A name whose seat is still offline (connection lost, token lost) takes that seat back; a name that is online is NAME_TAKEN. After a server restart the profiles, host and password come from the autosave, so a returning user gets the same role and color as before the restart.
9. Leaving the session. Session > Disconnect (LEAVE), a kick, and an auto-reconnect that gave up (60 s, also what a stopped server looks like) all end in `NetClient.disconnected(reason)`. The client then calls `app.reset_session()` and shows the connect dialog, pre-filled with the last server, room code and username, with the reason. The next join starts from `last_seq` 0 and a full SNAPSHOT.

## F. Python interfaces between owners

### Server

- `server/dispatcher.py` (P1): decorator `@handler('TYPE')` on `fn(session, msg)`. Before join a connection may send only CREATE_ROOM, JOIN_ROOM, PONG and ROOM_LIST_REQUEST; anything else gets ERROR NO_ROOM. `session.member` has name, role, color, token; `session.room`; `session.send(type, payload, **env)`; `session.send_error(code, text, ref_type)`.
- `server/rooms.py` (P1): Room has `.code`, `.members` (name to Member), `.lock` (RLock), `.board` (attached by P3), `.chat` (attached by P4), `.assets` (P3), `.broadcast(type, payload, exclude=None, **env)` and `.send_to(name, type, payload, **env)`. env may carry op_id, seq, user. Hooks run in registration order: `rooms.on_room_created(fn(room))`, `rooms.on_member_joined(fn(room, member, rejoin, last_seq))`, `rooms.on_member_left(fn(room, member))`. `Room.summary()` is one row of ROOM_LIST and `rooms.manager.summaries()` all of them.
- `server/presence.py` (P1): `broadcast_user_list(room)`. Call it after changing any member's role.
- `server/permissions.py` (P4): `can(role, action) -> bool`. Actions: draw, undo, upload, version_save, chat, cursor, clear, version_restore, version_delete, version_rename, set_role, kick.
- `server/board_state.py` (P3): BoardState with `.objects` (ordered dict), `.seq`, `.log`, `.apply(envelope)`, `.snapshot_payload()`, `.delta(from_seq)`.

### Client

- `client/app_state.py` (P1): AppState holds `.net`, `.me` (username, role, color, room, token), `.users`, `.board`, `.canvas`, `.window`, `.pen` (dict: color, width, tool, alpha), `.active_tool` and `.toast(text)`; Qt signals `users_changed`, `dm_requested(str)` and `role_changed`. `.reset_session()` runs every `@on_session_reset` function, then clears `.me` and `.users`.
- `client/net_client.py` (P1): NetClient(QObject) with `connect_to(host, port)`, `send(type, payload, op_id=None)`, `disconnect()`; module function `fetch_room_list(host, port)` returns `(rooms, error)` over a short-lived connection of its own, for the join screen; signals `connected`, `disconnected(str)`, `reconnecting`, `reconnected`. Incoming messages reach handlers registered with `on_message`, on the GUI thread.
- `client/ops.py` (P1, frozen): `send_op(app, type, payload) -> op_id` creates the op_id, calls `app.board.apply_local(...)`, then `app.net.send(...)`. Every board change by every tool (P2, P3, P4) goes through it; nobody sends OP_* with net.send directly.
- `client/board_model.py` (P3): BoardModel(QObject) with `.objects` (ordered dict), `.last_seq`, `.apply_local(type, payload, op_id)`, `.apply_remote(msg)`, `.load_snapshot(payload)` and signal `changed(obj_id_or_None)`.
- `client/canvas.py` (P2): CanvasWidget with `screen_to_board(pt)`, `board_to_screen(pt)`, `snap(pt)`, `zoom`, `offset` and signal `cursor_moved(float, float)`.
  Also: `show_grid` / `set_show_grid(on)`, `snap_enabled` / `set_snapping(on)`.
- Tool classes (registered with `register_tool`): attributes `id`, `label`, `shortcut`, `group`; methods `on_press(canvas, p, ev)`, `on_move(canvas, p, ev)`, `on_release(canvas, p, ev)`, `cursor()`. p is in board units and already snapped when snapping is on.
- Renderers: `register_renderer(kind, fn(painter, obj, ctx))`. `render.render_board(painter, objects, ctx)` paints in z order; P3's exports reuse it on a QImage or a QPdfWriter.

## G. Registries and bootstrap (client/registry.py, frozen)

`register_tool(cls)`, `register_renderer(kind, fn)`, `register_editor(kind, fn(app, canvas, obj))` (called by the select tool on double-click), `register_panel(name, factory(app), area, title)`, `register_menu_action(menu, label, fn(app), shortcut=None)`, `register_shortcut(keyseq, fn(app))`, the decorator `on_message(type)` for `fn(app, msg)`, and the decorator `on_session_reset` for `fn(app)`: it empties a module's per-room state (board, chat, versions) when the user leaves the room. BoardModel also has `.reset()`.

`server/bootstrap.py` and `client/bootstrap.py` import every module with plain import statements (PyInstaller needs static imports), in this order. The server's last line registers `versions.send_versions_on_join` as a join hook after chat, so VERSIONS follows CHAT_HISTORY.

- Server: dispatcher, rooms, presence (P1); permissions (P4); board_state, board_ops, undo, sync, versions, assets (P3); chat (P4). Chat stays last so the join order is presence, snapshot, chat history.
- Client: registry, app_state, net_client, board_model, render, render_draw, render_text, render_image, canvas, toolbar, tools.pen_tool, tools.shape_tools, tools.select_tool, tools.text_tool, tools.image_tool, shortcuts, draw_sync, chat_panel, users_panel, stats_panel, history_panel, cursor_overlay, toasts, export, connect_dialog, main_window.

## H. File map

Create every file as a stub with `# OWNER: P<n>` on line 1. Package folders need an empty `__init__.py`, owned by P1.

| Owner | Files |
|---|---|
| P1 | run_server.py, run_client.py, requirements.txt, .gitignore, build scripts, packaging/ (WhiteboardServer.spec, WhiteboardClient.spec, WhiteboardClient-console.spec, README_RUN.txt), common/app_paths.py, scripts/a_exe_smoke.py, docs/CONTRACT.md, common/config.py, common/framing.py, common/protocol.py, common/models.py, server/server.py, server/dispatcher.py, server/rooms.py, server/presence.py, server/bootstrap.py, client/net_client.py, client/registry.py, client/app_state.py, client/ops.py, client/render.py, client/bootstrap.py, client/connect_dialog.py, client/users_panel.py, client/stats_panel.py, client/toasts.py, client/cursor_overlay.py |
| P2 | common/kinds_draw.py, client/canvas.py, client/toolbar.py, client/shortcuts.py, client/draw_sync.py, client/render_draw.py, client/tools/pen_tool.py, client/tools/shape_tools.py, client/tools/select_tool.py |
| P3 | common/kinds_image.py, server/board_state.py, server/board_ops.py, server/undo.py, server/sync.py, server/versions.py, server/assets.py, client/board_model.py, client/history_panel.py, client/export.py, client/asset_cache.py, client/render_image.py, client/tools/image_tool.py |
| P4 | common/kinds_text.py, server/chat.py, server/permissions.py, client/chat_panel.py, client/main_window.py, client/render_text.py, client/tools/text_tool.py, scripts/role_attack.py, docs/final/ |
| Each person | docs/sections/p<n>_<theme>.md (own file only) |
