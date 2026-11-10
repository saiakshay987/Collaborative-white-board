# Final technical report: outline

Working document for the final technical PDF. It lists the chapters, what each one must say, which code backs it, and which `docs/sections/` file feeds it. Remove the sections marked *(internal)* before the PDF is produced.

**Basis and gaps.**
- The chapter headings are the ones requested for the report; the brief itself is not in the repository, so if it names other headings or a page limit, adjust this file first.
- Only `docs/sections/p4_chat_access.md` exists today. Sections of P1, P2 and P3 follow the contract's file-map pattern `docs/sections/p<n>_<theme>.md`; their real names are not known yet, so they are written below as `p1_*.md`, `p2_*.md`, `p3_*.md`.
- Every code reference was checked against `main` (contract rev 3). Behaviour that exists nowhere in the code is listed under *Open points* and must not be described as done.

## Who writes what

| Owner | Area (from the contract's file map) | Section file |
|---|---|---|
| P1 Sharwan | transport and framing (`common/config.py`, `framing.py`, `protocol.py`, `models.py`), server core (`server/server.py`, `dispatcher.py`, `rooms.py`, `presence.py`, `bootstrap.py`), client connection (`client/net_client.py`, `registry.py`, `app_state.py`, `ops.py`, `connect_dialog.py`, panels for users, stats, toasts, cursors) | `docs/sections/p1_*.md` |
| P2 Akshay | drawing (`common/kinds_draw.py`, `client/canvas.py`, `toolbar.py`, `shortcuts.py`, `draw_sync.py`, `render_draw.py`, `client/tools/pen_tool.py`, `shape_tools.py`, `select_tool.py`) | `docs/sections/p2_*.md` |
| P3 Janith | board state and history (`server/board_state.py`, `board_ops.py`, `undo.py`, `sync.py`, `versions.py`, `assets.py`; `client/board_model.py`, `history_panel.py`, `export.py`, `asset_cache.py`, `render_image.py`, `tools/image_tool.py`) | `docs/sections/p3_*.md` |
| P4 Harshith | chat and access (`server/permissions.py`, `server/chat.py`, `client/chat_panel.py`, `client/main_window.py`, text and sticky: `common/kinds_text.py`, `client/render_text.py`, `client/tools/text_tool.py`, `scripts/role_attack.py`) | `docs/sections/p4_chat_access.md` (exists) |

---

## Front matter (not numbered)

- Title page: project name (LAN-only collaborative whiteboard, Computer Networks lab), team with the four names and roles, date (deadline 13 Nov 2026).
- Abstract (half a page): what the system does, the transport (TCP, length-prefixed JSON frames), and the three ideas the report develops: a server that is the single authority, sequence-numbered operations, and recovery by token and replay.
- Contents.

---

## 1. System architecture

**Purpose.** Show the parts, how they are started, how they are plugged together and who owns what.

**Cover**
- Two programs. Server: `run_server.py` imports `server/bootstrap.py` (registers every handler and room hook), then `server/server.py` accepts connections. Client: `run_client.py` imports `client/bootstrap.py`, creates `AppState`, `NetClient` and `BoardModel`, shows `ConnectDialog`, then `MainWindow`.
- Shared package `common/`: `config.py`, `framing.py`, `protocol.py`, `models.py` and the `kinds_*.py` validators.
- Extension by registries only: client `register_tool`, `register_panel`, `register_menu_action`, `register_renderer`, `register_editor`, `register_shortcut`, `@on_message` (`client/registry.py`); server `@handler` (`server/dispatcher.py`) and the room hooks `on_room_created`, `on_member_joined`, `on_member_left` (`server/rooms.py`). Imports are static because of PyInstaller; the import order in both `bootstrap.py` files is part of the design (it fixes the order of join hooks).
- Server model: `RoomManager` -> `Room` (members, `lock`, and `board`, `undo`, `versions`, `chat` attached by hooks) -> `Member` (name, role, colour, token, session).
- Authority: the server sets user, role, seq and ts and calls `permissions.can(role, action)` before changing anything; the client only requests.

**Sources**

| Owner | Takes |
|---|---|
| P1 | overall block diagram, process and module overview, registries, bootstrap order, room model |
| P2 | canvas and tools as the drawing front end |
| P3 | `BoardState`, `UndoManager`, `VersionStore`, `BoardModel` and how they attach to a room |
| P4 | chat/permissions modules; main window as the shell that places registered panels (`p4_chat_access.md`, sections 1, 2 and 4) |

**Figures to make**: block diagram (clients, server, rooms, hooks); module-to-owner table.

---

## 2. Application message format

**Purpose.** Define precisely what goes on the wire.

**Cover**
- Frame (contract A): 4-byte big-endian unsigned length N, then N bytes of UTF-8 JSON; `config.MAX_FRAME` is 8 MiB, a larger N closes the connection (`common/framing.py`).
- Envelope (contract B): `v`, `type`, `room`, `user`, `op_id`, `seq`, `ts`, `payload`; which fields only the server may set (`user`, `seq`, `ts`; the dispatcher overwrites them); `op_id` and `<user>-<counter>` ids (`protocol.new_op_id`).
- Message tables by family: C1 session and presence, C2 board operations, C3 state, history and images, C4 chat and access; error codes (BAD_FORMAT, NO_ROOM, BAD_PASSWORD, NAME_TAKEN, FORBIDDEN, TOO_LARGE, NOT_FOUND) and the `ERROR {code, text, ref_type}` reply.
- Board objects (contract D): kinds stroke, line, arrow, rect, ellipse, text, sticky, image; the `KINDS` registry and the optional `limits` of rev 3 checked by `models.validate_obj`.
- One worked example per family (a real captured frame is better than a table).

**Sources**

| Owner | Takes |
|---|---|
| P1 | frame, envelope, error codes, C1 (`framing.py`, `protocol.py`, `models.py`) |
| P2 | draw kinds and the stroke-part message (C2, `kinds_draw.py`) |
| P3 | C2 and C3 (operations, SNAPSHOT, SYNC_*, VERSION_*, IMG_*) |
| P4 | C4 (CHAT, DM, CHAT_HISTORY, SET_ROLE) and the text and sticky kinds with their limits (`p4_chat_access.md`, sections 4, 7 and 8) |

**Evidence**: `scripts/p1_test_framing.py` and `scripts/p1_test_limits.py`; a hex dump of one frame.

---

## 3. Socket communication

**Purpose.** Explain how bytes become messages and how the server talks to one client or to all.

**Cover**
- TCP and why: reliable ordered byte stream; the application adds sequence numbers only to repair gaps after a dropped connection.
- Server: bind `0.0.0.0:config.PORT` (5050), `SO_REUSEADDR`, `listen(32)`, accept loop, one `ClientHandler` thread per connection (`server/server.py`).
- Framing in practice: why recv loops until exactly N bytes (a TCP read may return part of a frame), per-socket send lock, traffic counters (`framing.STATS`).
- Client: `NetClient` with a receiver thread, a sender thread fed by a queue, Qt signals to reach the GUI thread; `TCP_NODELAY` for small stroke frames; `PING` answered from the receiver thread (`client/net_client.py`).
- Liveness: `PING` every 5 s, `PONG`, timeout after 15 s without a frame, `STATS` every 2 s (`server/presence.py`, `Heartbeat`).
- Who receives what: `Room.broadcast` (everyone online, optionally excluding one name) versus `Room.send_to` (one member); CURSOR relayed to the others only; CHAT broadcast including the sender; DM unicast to recipient plus echo to sender (`p4_chat_access.md`, section 4).
- Join sequence as a message-sequence diagram: JOIN_OK, USER_LIST, SNAPSHOT or SYNC_DELTA, CHAT_HISTORY (`p4_chat_access.md`, section 5).

**Sources**

| Owner | Takes |
|---|---|
| P1 | transport, framing, server threads, heartbeat, `NetClient` threads |
| P3 | image upload and download in 24 KB chunks (contract C3), if implemented by then |
| P4 | broadcast versus unicast, join message order |

**Figures**: framing diagram; thread diagram (server and client); join sequence diagram.

---

## 4. Synchronization strategy

**Purpose.** Explain how every client ends up with the same board and the same chat.

**Cover**
- Server order is the truth: board operations are processed one at a time per room under `room.lock`; `seq` is stamped by the server (`board_ops._process`); accepted operations are broadcast to all including the sender (echo rule).
- Client (`client/board_model.py`): optimistic apply, matching by `op_id`, duplicate dropping, a gap buffer with a 1 s timer, `SYNC_REQUEST` at most every 2 s, a 5 s pending timeout.
- State transfer: SNAPSHOT, SYNC_DELTA when the gap is at most `config.MAX_DELTA` (500), `server/sync.py`; SNAPSHOT after VERSION_RESTORE.
- Undo and redo as ordinary inverse operations with fresh seq numbers (`server/undo.py`, `board_ops._process_history`).
- Stroke parts: batching and throttling of draw messages (P2, `client/draw_sync.py`; contract C2: at most 50 points every 40 ms).
- Chat: server-assigned `msg_id` and `ts`, store and broadcast under `room.lock` so all clients see one order; client sorts by `ts` and ignores known `msg_id`s (`p4_chat_access.md`, sections 4 and 6).
- Demonstration: `WB_DROP_PCT` and `WB_DUP_PCT` environment switches, and Debug > Show board checksum (`client/board_model.py`), `scripts/p3_sync_demo.py`.

**Sources**

| Owner | Takes |
|---|---|
| P3 | seq order, echo, gap buffer, SYNC, SNAPSHOT/DELTA, undo (main author of this chapter) |
| P2 | stroke part batching and throttled moves |
| P1 | `ops.send_op` as the single path for board changes |
| P4 | chat ordering and duplicate filtering |

---

## 5. Concurrency handling

**Purpose.** Show that the shared state is protected and what rules prevent deadlock and GUI freezes.

**Cover**
- Thread inventory. Server: accept loop, one reader thread per client, heartbeat thread, autosave thread. Client: GUI thread, receiver and sender threads, short-lived connect and reconnect workers, QTimers on the GUI thread.
- Locks and their purpose: `Room.lock` (an RLock; every change of room state happens under it), `RoomManager.lock` (only the rooms dict), per-socket send lock in `framing`, `ChatLog` own lock (so chat never blocks board operations), the save locks in `server/versions.py` (disk writes outside `room.lock`).
- Rules that were deliberately applied: broadcast inside the lock to keep wire order equal to seq order; error replies and heavy sends after the lock is released (SET_ROLE); permission checks before taking the lock; no disk I/O while holding `room.lock`; hooks isolated by `rooms._run_hooks`.
- Client rule: network data reaches widgets only through Qt signals and `@on_message` handlers; nothing blocks the GUI thread (examples: the sender queue, throttled `OP_UPDATE` in `select_tool.py`, trickled loading in `export.py`).
- Races found and handled: two hosts demoting each other (`on_set_role`, one locked block); recipient going offline during a DM (`on_dm`, checked and sent under the lock).

**Sources**

| Owner | Takes |
|---|---|
| P1 | threads, `Room.lock`, heartbeat, connection handling |
| P3 | the board pipeline under the lock, autosave locking |
| P4 | chat, DM and SET_ROLE locking (`p4_chat_access.md`, sections 4 and 7) |
| P2 | GUI-thread timers and throttling |

---

## 6. Recovery mechanisms

**Purpose.** Show what survives a dropped connection, a silent client and a server restart.

**Cover**
- Client connection loss: `reconnecting`, retry every 2 s for up to 60 s, `JOIN_ROOM` with token and `last_seq`; being kicked is not retried (`client/net_client.py`).
- Server side: a dropped client becomes offline but keeps its seat for `config.REJOIN_WINDOW_S` = 120 s; with the token it resumes the same role and colour (`RoomManager.join`); 15 s without a frame counts as lost; an empty room is deleted after 10 minutes (`rooms.ROOM_EMPTY_TTL_S`).
- What a rejoining client receives: SYNC_DELTA (or SNAPSHOT) and the full CHAT_HISTORY again, with duplicates filtered by `msg_id`.
- Persistence: autosave every 10 s to `server_data/<ROOM>/autosave.json`, written atomically (temporary file, then `os.replace`); versions saved on disk; rooms restored when the server starts (`server/versions.py`).
- What is **not** recovered: passwords, members and roles, chat and undo stacks; the first user to join a restored room becomes its host (`versions.py` docstring).
- Limits and failure cases to state honestly: no host succession, chat not persistent (`p4_chat_access.md`, section 10).

**Sources**

| Owner | Takes |
|---|---|
| P1 | heartbeat, rejoin window, `NetClient` reconnect |
| P3 | SYNC_DELTA/SNAPSHOT, autosave, versions, restore |
| P4 | chat history replay, role retained on resume, limitations |

**Evidence**: `scripts/p3_test_persistence.py`, `scripts/p3_test_versions.py`; a demo: kill the Wi-Fi or the client for 20 seconds and show the board and chat catching up.

---

## 7. User guide

**Purpose.** Let a new user run the system without help. Keep each step short and tested on a second computer.

### 7.1 Configure the server
- Requirements: Python 3.10 or later; `pip install -r requirements.txt` (PyQt5 is the only dependency).
- Start: `python run_server.py [port]`. It prints `Whiteboard server listening on <LAN IP>:<port> (LAN)`; Ctrl+C stops it. The server writes room data under `server_data/`.
- Source: P1 (`run_server.py`).

### 7.2 Connect over the LAN
- Start the client with `python run_client.py`; the connect dialog opens.
- Join tab: server IP (the address the server printed), room code (6 characters), username, password if the room has one. Plain-language errors for a wrong password, a taken name, an unknown room (`client/connect_dialog.py`).
- Source: P1.

### 7.3 Create or join a session
- Host tab: username and optional password; the dialog connects to the server on this PC, shows the room code and the LAN IP in large text and offers "Copy code and IP", then "Open whiteboard". The creator is the host; joiners are editors.
- Session menu: Copy room code, Leave, Reconnect, and (host) Manage roles (`client/main_window.py`). The status bar shows my name and role, the room and the server address; the title shows the room code.
- Source: P1 (dialog), P4 (`p4_chat_access.md`, sections 1 and 7).

### 7.4 Use the application
Group the text by task, one short paragraph and one screenshot each:

| Task | What to say | Owner |
|---|---|---|
| Drawing | pen, marker, highlighter, eraser; line, arrow, rectangle, ellipse; colour and width in the tool dock; per-tool memory | P2 |
| Select and edit | select, move, resize, delete, double-click to edit text | P2, P4 (editor) |
| Text and sticky notes | T and N; drag a box or click; Enter sends, Shift+Enter new line, Esc cancels | P4 (`p4_chat_access.md`, section 8) |
| View | zoom (Ctrl+wheel), scroll and pan, grid and snap (G), command bar (/), keyboard help (F1) | P2 |
| Undo and history | Ctrl+Z, Ctrl+Y; save a version (Ctrl+S) and the history panel; restore (host) | P2 shortcuts, P3 |
| Files | File > Save board as JSON; Load board from JSON (host only) | P3 |
| Chat | room tab, double-click a user for a private tab, unread badge | P4 |
| Roles | host sets roles in Session > Manage roles; a viewer sees the yellow "View only" banner and cannot draw; the host can remove a user from the users panel | P4, P1 |
| Status | users panel, live cursors, latency and traffic panel, Debug > Show board checksum | P1, P3 |
| Problems | what the toasts mean; reconnecting; leaving and rejoining | P1, P4 |

---

## Appendices (optional)

- A. Contract summary: the message tables of sections C1 to C4 and the object table of section D (copy from `docs/CONTRACT.md`).
- B. Test and demo scripts: `scripts/p1_test_framing.py`, `p1_test_limits.py`, `p1_test_client.py`, `p2_render_test.py`, `p3_test_board.py`, `p3_test_board_model.py`, `p3_test_versions.py`, `p3_test_persistence.py`, `p3_test_export.py`, `p3_test_history_panel.py`, `p3_sync_demo.py`, `p4_test_permissions.py`, `p4_test_text.py`, and `scripts/role_attack.py` for the access-control demo.
- C. Contribution of each member (only if the brief asks for it).

---

## Open points *(internal; resolve before writing, then delete)*

1. **Brief.** Check these headings and the user-guide items against the brief; the repository does not contain it.
2. **Section files.** P1, P2 and P3 have not written their `docs/sections/` files yet; agree on file names with them so the table above can name them.
3. **Image upload (P3).** `server/assets.py` is still a stub: there is no IMG_BEGIN or ASSET_GET handler, so chapters 2, 3 and 4 must not describe image transfer as working, and the `upload` permission is not enforced yet (`scripts/role_attack.py` shows IMG_BEGIN as FAIL for that reason).
4. **Export formats.** `client/export.py` only saves and loads JSON; `client/render.py` mentions image and PDF export. Confirm whether the brief requires PNG or PDF.
5. **Custom port.** `run_server.py` accepts a port argument, but `client/connect_dialog.py` always connects to `config.PORT`; either document "use the default port" or ask P1 to add a port field.
6. **Firewall and Wi-Fi.** The user guide should say what to open for port 5050 on the host's operating system; this has not been tested here and must be tried on the lab machines.
7. **LAN discovery.** The contract mentions an optional UDP discovery; nothing in the code implements it, so leave it out of the report.
8. **Open bug, P1.** `server/rooms.py`, `Room.broadcast`: the member list is copied under the lock, but `m.session` is read after the lock is released, so a member that goes offline in between gives `AttributeError: 'NoneType' object has no attribute 'send'`. It was seen in the heartbeat's STATS broadcast. Fix it before the demo or state it as a known limitation in chapter 5.
9. **P4 limitations to carry over.** No host succession and no persistent chat history (`p4_chat_access.md`, section 10); mention both where chapters 5 and 6 discuss recovery.
