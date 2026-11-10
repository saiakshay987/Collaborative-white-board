# P4 section: chat, access control and text objects

Owner: P4 (Harshith). Everything below is taken from the code on `main` (contract rev 3) and names the file and function where it happens. Where something is **not** implemented or only inferred from reading the code, the text says so.

Files of this section: `server/permissions.py`, `server/chat.py`, `client/chat_panel.py`, `client/main_window.py`, `common/kinds_text.py`, `client/render_text.py`, `client/tools/text_tool.py`, `scripts/role_attack.py`, tests `scripts/p4_test_permissions.py` (40 tests) and `scripts/p4_test_text.py` (27 tests).

---

## 1. Roles and permissions

`server/permissions.py` holds one table and one function, `can(role, action)`. It is the only place that decides what a role may do (contract C4).

| Action (what it covers) | host | editor | viewer |
|---|:---:|:---:|:---:|
| `draw` (OP_ADD, OP_STROKE_PART, OP_UPDATE, OP_DELETE) | yes | yes | no |
| `undo` (UNDO, REDO) | yes | yes | no |
| `upload` (IMG_*) | yes | yes | no |
| `version_save` (VERSION_SAVE) | yes | yes | no |
| `chat` (CHAT, DM) | yes | yes | yes |
| `cursor` (CURSOR) | yes | yes | yes |
| `clear` (OP_CLEAR) | yes | no | no |
| `version_restore` (VERSION_RESTORE) | yes | no | no |
| `set_role` (SET_ROLE) | yes | no | no |
| `kick` (KICK) | yes | no | no |

- **Default deny.** `can()` returns `False` for an unknown role, an unknown action, or a value of the wrong type (`None`, a list from a hostile client: the `TypeError` from the unhashable value is caught).
- **Who gets which role.** The first member of an empty room is the host, every later joiner is an editor (`server/rooms.py`, `RoomManager.join`, step 4: `ROLE_HOST if not room.members else ROLE_EDITOR`). Only `SET_ROLE` changes a role afterwards.
- **Reads need no permission.** `VERSION_LIST` and `SYNC_REQUEST` are open to every member, viewers included, because they change nothing (see their docstrings in `server/versions.py` and `server/sync.py`).

## 2. Where every permission check happens

The role that is checked is always `session.member.role`: the `Member` object the server keeps for the connection. It is never read from a message.

| Message | File and function | Action | Checked | Reply when refused |
|---|---|---|---|---|
| OP_ADD, OP_STROKE_PART, OP_UPDATE, OP_DELETE | `server/board_ops.py`, `_process()` | `draw` | first step, before payload validation and before `room.lock` | `ERROR FORBIDDEN` (ref_type = the op) |
| OP_CLEAR | `server/board_ops.py`, `_process()` via `on_op_clear` | `clear` | first step, so a refused clear also saves no automatic version | `ERROR FORBIDDEN` |
| UNDO, REDO | `server/board_ops.py`, `_process_history()` | `undo` | first step, before `room.lock` | `ERROR FORBIDDEN` |
| VERSION_SAVE | `server/versions.py`, `on_version_save()` via `_require()` | `version_save` | first line of the `try` | `ERROR FORBIDDEN` |
| VERSION_RESTORE | `server/versions.py`, `on_version_restore()` via `_require()` | `version_restore` | first line of the `try` | `ERROR FORBIDDEN` |
| KICK | `server/presence.py`, `on_kick()` | `kick` | first line, before touching `room.members` | `ERROR FORBIDDEN` |
| CURSOR | `server/presence.py`, `on_cursor()` | `cursor` | first line, before the rate limit | `ERROR FORBIDDEN` |
| SET_ROLE | `server/chat.py`, `on_set_role()` | `set_role` | **inside** `with room.lock`, together with the change | `ERROR FORBIDDEN` |
| CHAT | `server/chat.py`, `on_chat()` | `chat` | first line, before `room.lock` | `ERROR FORBIDDEN` |
| DM | `server/chat.py`, `on_dm()` | `chat` | first line, before `room.lock` | `ERROR FORBIDDEN` |
| IMG_BEGIN, IMG_CHUNK, IMG_END | none | `upload` | **not implemented yet**: `server/assets.py` is a stub, so there is no handler | `ERROR BAD_FORMAT "no handler for ..."` (from the dispatcher) |

Messages with no permission check by design: CREATE_ROOM and JOIN_ROOM (there is no role yet; name, password and token are checked instead), LEAVE and PONG (they only affect the sender's own seat or round-trip time), VERSION_LIST and SYNC_REQUEST (read only).

Three rules in `server/dispatcher.py` back this up:

1. `dispatch()` overwrites `msg["user"]`, `msg["room"]`, `msg["seq"]` and `msg["ts"]` before any handler runs, so a handler can trust `msg["user"]`.
2. A client that has not joined a room may only send CREATE_ROOM, JOIN_ROOM and PONG; everything else gets `ERROR NO_ROOM`.
3. A message type with no registered handler gets `ERROR BAD_FORMAT`. Types that only the server sends (JOIN_OK, USER_LIST, SNAPSHOT, CHAT_HISTORY, ...) have no handler, so a client cannot inject them.

**Order of the checks.** The permission is checked before the payload, so a client without the right gets `FORBIDDEN` and never learns anything about payload rules. For board operations the check sits outside `room.lock`; only `SET_ROLE` checks inside the lock. The consequence: the one operation a client's reader thread is already processing when a `SET_ROLE` demotion lands may still be applied (each connection has one reader thread, so its messages are handled one at a time). The next one is refused.

## 3. Why the client's "View only" is only cosmetic

What the client does for a viewer (all of it is convenience, none of it is protection):

- `client/main_window.py`, `ViewOnlyBanner`: a yellow strip over the canvas, shown by `_refresh_all()` when `app.me.role == "viewer"`. It is a `QLabel` that ignores the mouse.
- `client/toolbar.py` (P2), `_apply_role()`: disables the tool buttons and clears the active tool.
- `client/tools/pen_tool.py` `_can_draw()` (P2), used by the shape tools and by my text and sticky tools: shows the toast "Viewers cannot draw" and does nothing.
- `client/tools/select_tool.py` (P2): refuses to select or edit for a viewer.
- `client/main_window.py`, `open_manage_roles()`: shows a toast instead of the dialog unless I am the host.

Why this cannot be the protection: it all runs in a process the user controls, and `app.me.role` is just a variable. Someone who patches the client, or does not use it at all, can send any message. `scripts/role_attack.py` does exactly that with a raw socket and no GUI, and the server still answers `ERROR FORBIDDEN` because `permissions.can()` runs on the server with the role the server stored. The client-side checks only save the user a pointless request and an optimistic drawing the server would reject.

## 4. Chat: broadcast and unicast

`server/chat.py` keeps one `ChatLog` per room (`room.chat`, created by the `on_room_created` hook `attach_chat`).

**`ChatLog`**
- Its own `threading.Lock`, separate from `room.lock`, so chat never blocks board operations.
- Keeps the newest 500 messages in total (`MAX_KEPT`), room messages and DMs together.
- `msg_id` is `"m<counter>"`, `ts` is `time.time()`; both are set by the server (`_append`).
- `history_for(user)`: the last 200 room messages (`ROOM_HISTORY`) plus every DM where the user is sender or recipient, sorted by `ts` (stable sort, so equal timestamps keep arrival order).

**CHAT is a broadcast** (`on_chat`):
1. `permissions.can(role, "chat")`, else FORBIDDEN (viewers may chat).
2. The text must be a non-empty string after `strip()`, else BAD_FORMAT. It is cut silently to `MAX_CHAT_CHARS` = 1000.
3. Under `room.lock`: `room.chat.add_room(sender, text)` then `room.broadcast(CHAT, {msg_id, from, text, ts})` with no `exclude`. `Room.broadcast` sends to every online member, the sender included.

Why a broadcast: everybody in the room is meant to read it. Why the sender is included: the client does not draw its own message locally. The server echo is the single source of truth, so every window shows the same text, `msg_id`, `ts` and order, and a message the server refused never appears in the sender's window. Why store and broadcast under `room.lock`: two messages arriving on different reader threads cannot be delivered in a different order to different clients.

**DM is a unicast** (`on_dm`):
1. `can(role, "chat")`, else FORBIDDEN.
2. `to` must be a non-empty string other than the sender and the text non-empty after `strip()`, else BAD_FORMAT. Cut to 1000.
3. Under `room.lock`: the recipient must be in `room.members` (so in this room) **and online**, else `ERROR NOT_FOUND`. A DM to an offline user is refused, not queued.
4. `room.chat.add_dm(...)`, then `room.send_to(to, DM, out)` and `room.send_to(sender, DM, out)`: exactly two sockets, the same payload `{msg_id, from, to, text, ts}`.

A DM is never broadcast, so no other member's socket receives a byte of it. It is also never in anyone else's history: `ChatLog.history_for` only returns a DM when `m["from"] == user or m.get("to") == user`. This was tested with three real clients (A sends B a DM; C received nothing live, nothing in its `CHAT_HISTORY` after reconnecting, and a brand-new client D saw nothing either) and is covered by `DmHandlerTest.test_third_member_never_gets_it_not_even_in_history` in `scripts/p4_test_permissions.py`.

**Client side** (`client/chat_panel.py`): a tab widget with a permanent "Room" tab and one tab per DM partner. The same `ConversationView` code shows both. A DM tab opens by double-clicking a user (`app.dm_requested` -> `ChatPanel.open_dm_tab`) or automatically when a DM arrives; a DM that arrives while its tab is not in front adds an unread count to the tab label and calls `app.toast`. The input is disabled while disconnected or before a room is joined. Enter sends, Shift+Enter is a new line. Message text is HTML-escaped before display. If the server refuses a DM (`ERROR` with `ref_type` DM), a grey note is written into that DM tab.

## 5. What a client receives after joining, and why in this order

`server/presence.py`, `_seat()`, does the following while holding `room.lock`:

1. **JOIN_OK** `{room, token, role, color, users}` is sent to the joiner.
2. `rooms.manager.member_joined()` runs the `on_member_joined` hooks **in registration order**, which is the import order in `server/bootstrap.py`:
   1. presence (P1), `_announce_join`: `USER_JOINED` to the others, then **USER_LIST** to everyone including the joiner;
   2. sync (P3), `send_board_state`: **SNAPSHOT** on a first join, or `SYNC_DELTA` (or a SNAPSHOT) on a rejoin;
   3. chat (P4), `send_chat_history`: **CHAT_HISTORY** to the joiner only.

So the joiner sees: JOIN_OK, USER_LIST, SNAPSHOT (or SYNC_DELTA), CHAT_HISTORY. The reasons. The first two are my reading of what each message carries; the rest are stated in the code comments:

- JOIN_OK comes first because it carries the role, the token and the first user list.
- USER_LIST precedes the board and the chat because names and colours are needed to show them.
- The whole sequence runs under `room.lock`, and board operations also take it, so the board the joiner receives matches exactly the `seq` it continues from, and no operation can fall between the snapshot and the broadcasts after it (`server/sync.py`, `_send_state`).
- Chat is last (`bootstrap.py`: "Chat stays last"). The board is what matters for correctness, so a long history never delays it. Hooks are also isolated: `rooms._run_hooks` logs a failing hook and continues, "a P4 bug must not break P3's snapshot".

## 6. History replay and duplicates after a reconnect

The server does not track which chat messages a client already has. `send_chat_history` therefore runs on every join, **including a rejoin**, and sends everything `history_for(user)` returns (up to 200 room messages plus the user's own DMs).

The client removes the overlap (`client/chat_panel.py`):
- `ConversationView.add_message` keeps a set of the `msg_id`s it shows and ignores a message whose id is already there. The same method inserts a new message at its place in the list sorted by the server `ts` (`bisect`), re-rendering the view if the message is older than the last one.
- `ChatPanel.on_join_ok` clears the views only when the **room code changes**. After a reconnect to the same room the transcript stays, so the replayed history is mostly duplicates and is dropped.
- A DM from the history goes into the tab of the other person; it counts as unread only if it arrives live (`add_live_dm`), never from history.
- CHAT and CHAT_HISTORY can arrive while the connect dialog is still open, before this panel exists (`run_client.py` builds the window afterwards). The handlers park such messages in `_pending` and `create_chat_panel` replays them.

A reconnect itself is P1's mechanism (`client/net_client.py`: retry every 2 s for up to 60 s, JOIN_ROOM with the token; the server keeps the seat for `config.REJOIN_WINDOW_S` = 120 s). With the token the seat, role and colour are resumed, so a demoted viewer stays a viewer after reconnecting.

## 7. SET_ROLE and the last-host rule

`server/chat.py`, `on_set_role`. Inside one `with room.lock` block, in this order:

1. `can(role, "set_role")`, else FORBIDDEN.
2. `user` and `role` must be strings and `role` one of host, editor, viewer, else BAD_FORMAT.
3. The target must be in `room.members`, else NOT_FOUND. An offline member inside the rejoin window counts.
4. **Last-host rule**: if the target is a host, the new role is not host, and the number of members with role host in `room.members` is 1 or less, the answer is FORBIDDEN "the last host cannot be demoted". A host may promote others to host, so several hosts can exist.
5. Otherwise `target.role = new_role`.

After the lock is released, `presence.broadcast_user_list(room)` sends `USER_LIST` to everybody. There is no special message for the target: each client's `net_client._on_user_list` finds its own entry and calls `app.set_role`, which emits `role_changed`. That drives the banner, the tool dock and the toast "Your role is now viewer. ..." (`MainWindow._toast_role_change`). Errors go only to the sender.

Why the check and the change share one lock: two hosts demoting each other at the same moment cannot both pass the "last host" test, because the second one sees the first one's result. Error replies and the broadcast happen after the lock is released so network sends never hold the room.

**Manage roles (client, `client/main_window.py`).** The menu entry is registered with `registry.register_menu_action("Session", "Manage roles", open_manage_roles)`. `ManageRolesDialog` lists `app.users`, with a combo box (editor, viewer, host) per user. Choosing a role only sends `SET_ROLE {user, role}` through `app.net.send`; the dialog never changes a role itself. It rebuilds from the server's `USER_LIST` (`app.users_changed`) and also after an `ERROR` whose `ref_type` is SET_ROLE, so it never shows a role the server did not give. The combo box of the only host is disabled. A non-host gets the toast "Only the host can manage roles." instead of the dialog (the server would refuse anyway).

## 8. Text boxes and sticky notes

**Kinds** (`common/kinds_text.py`, contract D and rev 3). `text` and `sticky` have the same fields `{x, y, w, h, text, color, size}` (x, y, w, h numbers; text and color strings; size a number). Defaults: text has `text ""`, `color "#000000"`, `size 18`; sticky has `text ""`, `color "#fff176"` (its background), `size 16`. Limits, in the rev 3 shape `"limits": {field: [min, max]}`: `text` length 0 to 2000, `size` 8 to 72. `LIMITS` is the same dict exported for the client. `check(obj)` adds a colour format (`#rrggbb`) and `w, h >= 0` test, and `validate(obj)` runs `models.validate_obj` and then `check`.

**Server side.** Text and sticky travel as ordinary `OP_ADD` and `OP_UPDATE`, so they use P3's pipeline in `server/board_ops.py`: permission `draw`, then `clean_add` (which calls `models.validate_obj`: types and limits, and fills in the defaults) or `clean_update` and `check_update` (the object after applying `set` is validated again). A type or limit violation gives `ERROR BAD_FORMAT`; trying to change `id`, `kind`, `owner` or `z` through OP_UPDATE gives `ERROR FORBIDDEN`. The server never imports `kinds_text`; it only runs `models.validate_obj`, so the colour-format and `w, h >= 0` checks of `check()` are client-side only. A real server run confirmed: size 8 and 72 and 2000 characters accepted; size 7, size 73, 2001 characters, a string size, and the same violations through `OP_UPDATE` get BAD_FORMAT.

**Renderers** (`client/render_text.py`, registered with `register_renderer`):
- `render_text`: the text in the object's colour, word-wrapped inside the box, no background.
- `render_sticky`: a rounded rectangle (radius 8) in the object's colour with a thin darker edge and a soft shadow (three translucent rectangles offset by 3 units), dark text (`#2b2b2b`) inset by 10 units.
- Both clip to the box, break words longer than the box (`QTextOption.WrapAtWordBoundaryOrAnywhere`), use a pixel font size in board units (so wrapping does not depend on screen DPI), clamp the size to 8 to 72, and fall back to a default for a bad colour or a negative box. A renderer never raises on an odd object.

**Tools** (`client/tools/text_tool.py`, registered with `register_tool`, group `text`):
- **Text tool** (id `text`, key T): drag a box; a click without a drag makes a 240 x 80 box; a dragged box is at least 60 x 30. The colour is the dock's colour for this tool (black at first, `pen_defaults`), size 18.
- **Sticky tool** (id `sticky`, key N): a click makes a 200 x 160 note centred on the click, in the next of 6 pastel colours (`STICKY_PALETTE`, cycling). The palette only advances when a note is really sent. Size 16.
- Both do nothing for a viewer (toast "Viewers cannot draw"), and nothing is sent if the role becomes viewer while typing.

**Editor** (`InlineEditor`, a `QTextEdit` child of the canvas laid over the box, same font scaled by the zoom, same inset as the renderer so the text does not jump). Enter sends, Shift+Enter is a new line, Escape cancels, clicking away sends (a focus loss, except when another window is activated or a popup opens). Text is capped at 2000 characters while typing. A timer of 60 ms moves the overlay when the canvas is zoomed or panned. Only one editor is open at a time.

**Messages sent.** A new object: exactly one `OP_ADD {obj}` through `client/ops.send_op` (applied locally at once, confirmed by the server echo), with id `<user>-<n>` from `protocol.new_op_id`, validated first with `kinds_text.validate`. An empty text sends nothing. Editing: `register_editor("text")` and `register_editor("sticky")` register `edit_object`, which P2's select tool calls on a double-click. On finish it sends `OP_UPDATE {obj_id, set: {text}}`, only if the text changed and the object still exists (otherwise the toast "That object was deleted while you were editing it."). Moving and resizing are P2's select tool (`RESIZABLE` includes text and sticky); this code does not repeat them.

## 9. role_attack.py: what it proves

`scripts/role_attack.py` is a raw client built only on `common/framing.py` and `common/protocol.py`: no GUI, no client-side checks. Usage: `python scripts/role_attack.py --host IP --room CODE --name mallory` (missing values are asked for).

1. It joins the room (a new member is an editor) and prints its role.
2. It waits for Enter. In the demo the host opens Session > Manage roles and sets the script's user to viewer.
3. It checks the role the server reports in `USER_LIST`, warns and asks if it is not a viewer, then sends nine messages with **valid payloads**, so only the permission check can stop them: OP_ADD, OP_STROKE_PART, OP_DELETE, OP_CLEAR, UNDO, VERSION_RESTORE, SET_ROLE (promote itself to host), KICK (of a user that does not exist, so a missing check could not hurt anyone) and IMG_BEGIN.
4. Each `ERROR FORBIDDEN` for that message type is a green PASS. Anything else (accepted, another error code, no reply) is a red FAIL with the server's reply. Then a control: a viewer may chat, so a CHAT must be accepted.

While it waits for Enter, a reader thread answers the server's `PING` with `PONG`; without that the server would drop the connection after `config.TIMEOUT_S` (15 s). The exit status is 0 only if every attack was refused.

**Result on the current `main`:** eight PASS (OP_ADD, OP_STROKE_PART, OP_DELETE, OP_CLEAR, UNDO, VERSION_RESTORE, SET_ROLE, KICK) and one FAIL: IMG_BEGIN answers `BAD_FORMAT "no handler for IMG_BEGIN"` because `server/assets.py` is a stub. That FAIL is not a hole (nothing happens) but the `upload` permission is not exercised until P3 adds the handler.

**What it proves:** for those eight message types the server refuses a viewer by itself, whatever the client does. **What it does not prove:** it does not cover OP_UPDATE, REDO, VERSION_SAVE, IMG_CHUNK/IMG_END or CURSOR, and it does not test a role changing in the middle of an operation.

## 10. Known limitations

- **No host succession.** Only the first member of an empty room becomes host. If the last host leaves, times out or is kicked, nobody can clear the board, restore a version, kick or change roles in that room. `SET_ROLE` only stops demoting the last host; it cannot stop the host leaving. In a room restored from autosave nobody has a role yet, so the first user to join becomes host (`server/versions.py` docstring).
- **The last-host rule counts offline seats.** It counts every member in `room.members`, including a host seat that is offline inside the 120 s rejoin window.
- **No persistent chat history.** `ChatLog` lives in server memory only: at most 500 messages per room, not part of the autosave (`server/versions.py`: chat is not restored). It is lost when the server restarts or when a room has been empty for 10 minutes (`rooms.ROOM_EMPTY_TTL_S`). Clients keep what they have on screen only until they close.
- **Possible `msg_id` collision after a server restart (inferred from reading the code, not reproduced).** The counter in `ChatLog` restarts at 1 and the client ignores any id it already shows. A client that stays open across a server restart and rejoins a restored room with the same code would keep its old `m1`, and a new `m1` would be dropped as a duplicate.
- **DMs to offline users are refused**, not stored.
- **`upload` is not enforced** until the IMG_* handlers exist (P3, `server/assets.py`).
- **A role change is not instantaneous for an operation already past its check** (section 2).
- **Chat text over 1000 characters is cut silently**, without an error.
- **Text and sticky:** colour format and `w, h >= 0` are checked by the client only; there is no control for the font size (18 for text, 16 for sticky); clearing the text of an existing object sends an update, so a text object can become invisible.
- **Manage roles / Reconnect.** After Leave, or after NetClient gives up, Session > Reconnect sends `JOIN_ROOM {room, username}` without a password, so a password-protected room refuses it.

## 11. Ten likely viva questions

**1. A student changes the client so the drawing tools work for a viewer. What stops them?**
The server. `board_ops._process()` calls `permissions.can(session.member.role, 'draw')` as its first step. The role is the one stored in the server's `Member` object, so the patched client changes nothing. The answer is `ERROR FORBIDDEN`, no state change, no broadcast. `role_attack.py` demonstrates it with a client that has no GUI at all.

**2. How does the server know who sent a message, and what role they have?**
The connection owns a `Member` (`session.member`) created at join time by `rooms.manager.join`. `dispatcher.dispatch()` overwrites `msg["user"]`, `room`, `seq` and `ts` before the handler runs. The role changes only in `on_set_role`. Fields like `from` or `msg_id` that a client puts in a CHAT payload are ignored (`on_chat` builds the message with `room.chat.add_room`; tested in `ChatHandlerTest.test_sender_name_is_not_taken_from_payload`).

**3. CHAT is a broadcast and DM is not. What exactly is the difference in your code?**
`on_chat` calls `room.broadcast(CHAT, ...)`, which loops over every online member. `on_dm` calls `room.send_to` twice: once for the recipient, once as an echo to the sender. A broadcast costs one send per member; a DM costs two sends and the other members' sockets are never written. Broadcast is right when everyone should read it; unicast is how privacy is kept.

**4. How do you guarantee that a third user never sees a DM, even after reconnecting?**
Live: the DM is only sent to two sockets. Later: `ChatLog.history_for(user)` returns a DM only if the user is its sender or its recipient, and `send_chat_history` calls it with the joining member's own name on every join and rejoin. A test with three real clients and `test_third_member_never_gets_it_not_even_in_history` cover it.

**5. Why does the sender wait for the server echo instead of showing its own message?**
So there is one source of truth. The echo carries the server's `msg_id`, `ts` and position in the order, so every window is identical, and a message the server refused (for example FORBIDDEN or BAD_FORMAT) never shows up in the sender's window. The same idea is used for board operations (contract C2 echo rule), though those are applied optimistically.

**6. What is the last-host rule and why is it under `room.lock`?**
The last host cannot be demoted, otherwise nobody could manage the room. The count of hosts and the change of `target.role` happen in one locked block, so two hosts demoting each other at the same time cannot both succeed. The rule does not stop the last host from leaving (no host succession), and it counts offline seats.

**7. In which order does a client receive messages after joining, and why is chat last?**
JOIN_OK, then USER_LIST, then SNAPSHOT (or SYNC_DELTA), then CHAT_HISTORY. They are sent by the join hooks in `bootstrap.py` import order, under `room.lock`, so the board matches the `seq` the client continues from. Chat is last because the board is needed for correctness and a long history must not delay it, and because a failing hook is isolated by `rooms._run_hooks` and cannot break the snapshot.

**8. After a reconnect the server sends the chat history again. Why are there no duplicates on screen?**
The server cannot know what the client already has, so it resends everything (`send_chat_history` runs on rejoin too). `ConversationView.add_message` keeps a set of `msg_id`s and ignores known ones, and inserts new ones in `ts` order. `ChatPanel.on_join_ok` only clears the view if the room code changed.

**9. How do you stop a hostile client from storing a text of 10 MB or a font size of 5000?**
Contract rev 3 limits in `common/kinds_text.py`: `"limits": {"text": [0, 2000], "size": [8, 72]}`. The server runs `models.validate_obj` on every `OP_ADD` (`clean_add`) and on the object that results from every `OP_UPDATE` (`check_update`), answering BAD_FORMAT. The client also caps typing at 2000 characters and validates before sending, but that is only so a tool does not send what the server will refuse. Colour format and negative sizes are not checked by the server; the renderer falls back to safe values.

**10. What does `role_attack.py` prove, and what does it not?**
It proves that, for eight message types, a client that has been made a viewer is refused with FORBIDDEN by the server, with valid payloads so that only the permission check can be the reason. A CHAT control shows the server does not just refuse everything. It does not cover OP_UPDATE, REDO, VERSION_SAVE or CURSOR, and IMG_BEGIN currently fails only because the handler does not exist yet. While it waits for the host to demote it, a reader thread answers PING with PONG so the server does not time it out after 15 s.
