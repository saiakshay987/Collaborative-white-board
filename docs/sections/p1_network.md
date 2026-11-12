# OWNER: P1

# P1 section: transport, sessions, liveness and the client connection

Owner: P1 (Sharwan). Everything below was checked against the code on `main` (contract rev 3) and names the file and function where it happens. Numbers marked **measured** come from runs made on 11 Nov 2026 with the code on `main`. Numbers marked **estimated** are calculated, not measured. Fields marked **TO FILL** need values from the real lab test.

Report chapters fed by this file: 1 (architecture), 2 (message format), 3 (socket communication), 5 (concurrency), 6 (recovery) and user guide 7.1 to 7.3.

---

## 1. Architecture overview

### 1.1 Block diagram

The diagram is plain ASCII so it can be redrawn for the slides.

```
  Laptop A (host, also runs the server)         Laptop B, C, ... (joiners)
 +-------------------------------------+       +-------------------------------+
 | run_client.py                       |       | run_client.py                 |
 |  GUI thread (Qt event loop)         |       |  (same structure as A)        |
 |   ConnectDialog -> MainWindow       |       |                               |
 |   canvas, tools, panels             |       |                               |
 |   @on_message handlers <---+        |       |                               |
 |   NetClient (QObject)      | Qt     |       |                               |
 |    net.send -> queue       | signal |       |                               |
 |     net-send thread        |        |       |                               |
 |     net-recv thread -------+        |       |                               |
 +--------|----------------------------+       +---------------|---------------+
          | TCP 127.0.0.1:5050                                 | TCP <LAN IP>:5050
          | frames: [4-byte length][UTF-8 JSON]                |
 +--------v----------------------------------------------------v---------------+
 | run_server.py  (one process, binds 0.0.0.0:5050)                            |
 |                                                                             |
 |  main thread: Server.serve_forever()  accept() -> start ClientHandler       |
 |                                                                             |
 |  ClientHandler thread  x N  (one per TCP connection)                        |
 |     recv_msg() -> dispatcher.dispatch() -> @handler("TYPE") fn(session,msg) |
 |                                                                             |
 |  Heartbeat thread (presence.py): PING 5 s, STATS 2 s, timeout 15 s, sweep   |
 |  autosave thread (versions.py, P3): autosave.json every 10 s                |
 |                                                                             |
 |  rooms.manager (RoomManager, lock: rooms dict)                              |
 |     Room "K7QX2M"  lock = RLock                                             |
 |        members: name -> Member(name, role, color, token, session)           |
 |        board (P3)   undo (P3)   versions (P3)   assets (P3)   chat (P4)     |
 +-----------------------------------------------------------------------------+
```

There are two programs and one shared package:

- **Server** (`run_server.py`): imports `server/bootstrap.py`, which registers every handler and hook, then runs `server.Server` (`server/server.py`).
- **Client** (`run_client.py`): imports `client/bootstrap.py`, builds `AppState`, `NetClient` and `BoardModel`, shows `ConnectDialog`, then `MainWindow`.
- **`common/`**: used by both programs. It holds `config.py` (every shared constant), `framing.py` (frames on TCP), `protocol.py` (message types, envelope builder, op_id) and `models.py` (object validation over the `kinds_*.py` registries).

The server is the single authority. Clients only send requests. The server checks them, decides the result, numbers it, and tells everyone.

### 1.2 Where each P1 module sits

| Layer | Module | Job |
|---|---|---|
| shared | `common/config.py` | PORT 5050, MAX_FRAME 8 MiB, HEARTBEAT_S 5, TIMEOUT_S 15, REJOIN_WINDOW_S 120, MAX_DELTA 500, board size, 8 user colours |
| shared | `common/framing.py` | `send_msg`, `recv_msg`, `_recv_exact`, `encode_msg`, per-socket send lock, `STATS` counters |
| shared | `common/protocol.py` | type constants, `MESSAGE_TYPES`, `BOARD_OPS`, error codes, `make_msg`, `new_op_id` |
| shared | `common/models.py` | `validate_obj` over the merged `KINDS` registry, including rev 3 `limits` |
| server | `server/server.py` | `Server` (bind, listen, accept loop) and `ClientHandler` (one thread per connection, the `session` object) |
| server | `server/dispatcher.py` | `@handler(type)` registry and `dispatch()`: shape check, NO_ROOM gate, overwrites server-owned fields |
| server | `server/rooms.py` | `Member`, `Room` (lock, `broadcast`, `send_to`), `RoomManager` (`create_room`, `join`, `member_left`, `sweep`), hooks |
| server | `server/presence.py` | CREATE_ROOM, JOIN_ROOM, LEAVE, PONG, KICK, CURSOR handlers; USER_LIST, USER_JOINED, USER_LEFT; `Heartbeat` thread; STATS |
| server | `server/bootstrap.py` | static imports in hook order |
| client | `client/net_client.py` | `NetClient`: connect, sender and receiver threads, auto-reconnect, JOIN_OK, USER_LIST and USER_LEFT (kick) handlers |
| client | `client/registry.py` | `register_tool`, `register_panel`, `register_menu_action`, `register_renderer`, `register_editor`, `register_shortcut`, `on_message`, `deliver_message` |
| client | `client/app_state.py` | `AppState` (`.net`, `.me`, `.users`, `.board`, ...) and its Qt signals |
| client | `client/ops.py` | `send_op`: the single path for every board change (optimistic apply, then send) |
| client | `client/connect_dialog.py` | Host and Join tabs, friendly ERROR texts, network hint |
| client | `users_panel.py`, `stats_panel.py`, `cursor_overlay.py`, `toasts.py` | presence UI (section 6) |

### 1.3 Threads

**Server**

| Thread | Created in | Blocks on | Does |
|---|---|---|---|
| main | `run_server.main` → `Server.serve_forever` | `accept()` with a 1 s timeout (`ACCEPT_POLL_S`) so Ctrl+C works | accepts, sets `TCP_NODELAY` and a 20 s socket timeout, starts a `ClientHandler` |
| `client-<ip>:<port>` × N | `ClientHandler.run` | `framing.recv_msg` | reads frames, sets `last_rx`, calls `dispatcher.dispatch`. Every handler for this client runs on this thread |
| `heartbeat` | `presence.start_heartbeat` | `time.sleep(TICK_S = 1 s)` | timeouts, PING, STATS, `rooms.manager.sweep` |
| `autosave` (P3) | `server/versions.py` | sleep | writes `server_data/<ROOM>/autosave.json` every 10 s |
| `threading.Timer` | `ClientHandler.close_after_flush` | timer | hard close 2 s after a KICK if the client does not close |

**Client**

| Thread | Created in | Does |
|---|---|---|
| GUI thread | `QApplication.exec_` | every widget and every `@on_message` handler; QTimers (cursor throttle, stroke flush, stats sampling) |
| `net-recv` | `NetClient._start_link` | blocks in `recv_msg`; answers PING with PONG directly; emits `_frame(gen, msg)` |
| `net-send` | `NetClient._start_link` | drains `link.out` (a `queue.Queue`) with `framing.send_msg` |
| `net-connect` / `net-reconnect` | `connect_to` / `_on_lost` | short-lived: one connect, or the 2 s × 60 s retry loop |

The GUI thread never touches a socket. `NetClient.send` only puts the message on a queue. A received frame crosses to the GUI thread as a queued Qt signal (`_frame`, emitted on `net-recv` and delivered on the GUI thread because `NetClient` lives there). Every connection has a **generation number** (`_Link.gen`), so a late signal from a dead socket is ignored in `_on_frame` and `_on_lost`.

### 1.4 Registries and bootstrap

Features plug in through registries, so no owner edits another owner's file:

- Server: `@handler("TYPE")` (`dispatcher.handler`) allows exactly one handler per type. A second handler, or a type that is not in the contract, fails at import time. The room hooks are `rooms.on_room_created(fn(room))`, `on_member_joined(fn(room, member, rejoin, last_seq))` and `on_member_left(fn(room, member))`. `rooms._run_hooks` catches each hook's exception, so a bug in one owner's hook cannot stop the others.
- Client: `register_tool`, `register_panel`, `register_menu_action`, `register_renderer`, `register_editor`, `register_shortcut`, and `@on_message(type)`, which allows several handlers per type. `registry.deliver_message` calls them in order and catches each one's exception.

Both `bootstrap.py` files use plain `import` statements, because PyInstaller only finds static imports. **The order is part of the design.** Hooks run in registration order, which is import order: `presence` → P3 (`sync`, `versions`, `assets`) → `chat`. A joiner therefore always receives `JOIN_OK`, `USER_LIST`, `SNAPSHOT`/`SYNC_DELTA` and `CHAT_HISTORY`, in that order.

---

## 2. Application message format

### 2.1 Why a 4-byte length prefix

TCP delivers a stream of **bytes**, not messages. One `send()` can arrive split over several `recv()` calls, and two sends can arrive glued together in one. The application has to put the message boundaries back. The options were:

| Option | Problem |
|---|---|
| one message per `recv()` | wrong: TCP keeps no boundaries |
| delimiter (e.g. newline) | the delimiter may appear inside the data (chat text, base64 image chunks) and needs escaping; the receiver must scan every byte |
| **length prefix** (chosen) | the receiver knows exactly how many bytes to wait for, so there is no scanning or escaping. The size can be checked **before** anything is read or allocated |

Frame layout (`common/framing.py`, `_HEADER = struct.Struct("!I")`):

```
+--------------------------------+-----------------------------------+
| N: 4 bytes, unsigned, big-endian | N bytes of UTF-8 JSON (one object) |
+--------------------------------+-----------------------------------+
```

`!` means network byte order (big-endian), which is the same on every machine. `I` is a 32-bit unsigned integer.

### 2.2 Partial `recv` (`framing._recv_exact`)

`recv_msg` reads exactly 4 bytes, unpacks N, checks N, then reads exactly N bytes. Both reads go through `_recv_exact(sock, n)`, which loops:

```python
buf = bytearray(n); view = memoryview(buf); got = 0
while got < n:
    r = sock.recv_into(view[got:], n - got)   # may return fewer than asked
    if r == 0:                                # peer closed (FIN)
        if got == 0: return None              # clean close between frames
        raise ProtocolError("connection closed mid-frame")
    got += r
```

- `recv_into` writes into one preallocated buffer, so no partial pieces are concatenated.
- `recv` returning 0 bytes means the peer sent FIN. At a frame boundary that is a normal close (`None`). In the middle of a frame it is a `ProtocolError`.
- Sending is the mirror image. `encode_msg` builds the whole frame first. `send_msg` then calls `sock.sendall(frame)` while holding the **per-socket lock** (`_lock_for(sock)`, a `WeakKeyDictionary` of locks). `sendall` loops until the kernel has taken every byte, and the lock stops two threads from interleaving frames on one socket. For example, the heartbeat's PING and a broadcast from another client's thread can go to the same socket at the same time.
- `scripts/p1_test_framing.py` sends 1800 frames, including split and glued ones and frames of several MB. It passes (`ALL PASSED in 1.4 s`, measured).

### 2.3 MAX_FRAME

`config.MAX_FRAME = 8 MiB`. In `recv_msg` the check `n > config.MAX_FRAME` comes **before** the body is read. A broken or hostile header such as `ff ff ff ff` (4 GiB) is therefore rejected without allocating anything, and the connection is closed (`ProtocolError` in `ClientHandler.run`). `encode_msg` refuses to build such a frame on the sending side too. 8 MiB is far above the largest legitimate message (a SNAPSHOT of a full board is zlib-compressed, and image chunks are 24 KB). Other bad bodies also raise `ProtocolError`: invalid UTF-8, invalid JSON, or JSON that is not an object.

### 2.4 Real hex dump: JOIN_ROOM

This frame is exactly what `NetClient.send(protocol.JOIN_ROOM, {"room": "K7QX2M", "username": "alice"})` builds on a first join. It was generated with `protocol.make_msg` and `framing.encode_msg` from `main`. `room` is still `null` because the client is not in a room yet.

```
0000  00 00 00 82 7b 22 76 22 3a 31 2c 22 72 6f 6f 6d  ....{"v":1,"room
0010  22 3a 6e 75 6c 6c 2c 22 75 73 65 72 22 3a 6e 75  ":null,"user":nu
0020  6c 6c 2c 22 6f 70 5f 69 64 22 3a 6e 75 6c 6c 2c  ll,"op_id":null,
0030  22 73 65 71 22 3a 6e 75 6c 6c 2c 22 74 73 22 3a  "seq":null,"ts":
0040  30 2e 30 2c 22 74 79 70 65 22 3a 22 4a 4f 49 4e  0.0,"type":"JOIN
0050  5f 52 4f 4f 4d 22 2c 22 70 61 79 6c 6f 61 64 22  _ROOM","payload"
0060  3a 7b 22 72 6f 6f 6d 22 3a 22 4b 37 51 58 32 4d  :{"room":"K7QX2M
0070  22 2c 22 75 73 65 72 6e 61 6d 65 22 3a 22 61 6c  ","username":"al
0080  69 63 65 22 7d 7d                                ice"}}
```

- The header `00 00 00 82` is N = 0x82 = **130** bytes of body, so the frame is 134 bytes on the wire (plus TCP/IP headers).
- The body is compact JSON (`separators=(",", ":")`) with `ensure_ascii=False`, so names in other scripts travel as real UTF-8.
- A reconnect JOIN_ROOM adds `token` (32 hex characters) and `last_seq` and is 195 bytes.

### 2.5 Envelope (contract B)

```json
{"v":1,"type":"OP_ADD","room":"K7QX2M","user":"alice","op_id":"alice-41","seq":null,"ts":0.0,"payload":{}}
```

| Field | Set by | Code |
|---|---|---|
| `v` | both, always 1 | `protocol.VERSION` |
| `type` | sender; must be in `protocol.MESSAGE_TYPES` | `make_msg` raises `ValueError` otherwise |
| `room` | server (`ClientHandler.send` stamps it; `dispatch` overwrites incoming) | |
| `user` | **server only**: `dispatch` overwrites it with `session.member.name` | a client cannot pretend to be someone else |
| `op_id` | client, on board ops only: `protocol.new_op_id(user)` → `"alice-41"` | counter under `_op_lock`; used for dedupe and to match the echo |
| `seq` | **server only**, on accepted board ops (P3, `board_ops._process`) | `dispatch` resets incoming `seq` to `None` |
| `ts` | **server only**: `dispatch` sets receive time, `ClientHandler.send` stamps send time | one clock, the server's |
| `payload` | sender; must be a JSON object | `dispatch` answers BAD_FORMAT otherwise |

`make_msg` also raises `TypeError` for an unknown envelope field, so no off-contract field can get onto the wire.

### 2.6 Error codes

The reply is `ERROR {code, text, ref_type}`, sent to the sender only (`ClientHandler.send_error`). `ref_type` names the message that failed, so the client knows what it refers to. For example, `connect_dialog._on_error` shows errors for CREATE_ROOM or JOIN_ROOM in the dialog and turns every other error into a toast.

| Code | P1 sources |
|---|---|
| BAD_FORMAT | not an envelope, payload not an object, unknown type or no handler, a handler raised (`dispatch`); bad username or password type (`rooms.clean_username`, `clean_password`); CREATE or JOIN while already in a room (`presence._already_seated`); KICK without a name or kicking yourself; CURSOR without numbers |
| NO_ROOM | any non-join message before joining (`dispatch`); code with a wrong format (`rooms.clean_code`); unknown code; room deleted in the meantime (`RoomManager.join`, `room.closed`) |
| BAD_PASSWORD | `Room.check_password` failed (`RoomManager.join`, step 2) |
| NAME_TAKEN | the name is already a seat in the room, online or offline inside the rejoin window, and no matching token was given (step 3) |
| FORBIDDEN | `permissions.can(role, "kick" / "cursor")` is False |
| NOT_FOUND | KICK names no member |
| TOO_LARGE | not raised by P1 handlers (P3 board limits); an oversized frame is not answered, the connection is closed |

### 2.7 C1 messages (session and presence)

| Type | Dir | Payload | Server code |
|---|---|---|---|
| CREATE_ROOM | C→S | {username, password?} | `presence.on_create_room` → `RoomManager.create_room` → `_seat` |
| JOIN_ROOM | C→S | {room, username, password?, token?, last_seq?} | `presence.on_join_room` → `_seat` → `RoomManager.join` |
| JOIN_OK | S→C | {room, token, role, color, users} | sent in `_seat`, under `room.lock`, before the join hooks |
| LEAVE | C→S | {} | `presence.on_leave`: seat removed, reason `leave` |
| USER_LIST | S→C | {users:[{name, color, role, online}]} | `presence.broadcast_user_list` (after every join, leave and role change) |
| USER_JOINED | S→C | {name, color} | `presence._announce_join` hook, to everyone **except** the joiner |
| USER_LEFT | S→C | {name, color, reason} | `presence._announce_leave` hook; reason `leave`, `timeout` or `kicked` |
| PING | S→C | {t} | `Heartbeat.tick` every 5 s, to every connection |
| PONG | C→S | {t} | `presence.on_pong`: RTT sample (allowed before joining) |
| STATS | S→C | {rtt:{name:ms}, server:{clients, msgs_in, msgs_out, bytes_in, bytes_out}} | `presence.broadcast_stats` every 2 s |
| CURSOR | C→S→others | {x, y} | `presence.on_cursor`: rate limit, clamp, relay with `user` |
| KICK | C→S | {user} | `presence.on_kick`, host only |
| ERROR | S→C | {code, text, ref_type} | `ClientHandler.send_error` |

---

## 3. Session lifecycle

### 3.1 Create

1. The Host tab (`ConnectDialog._start`) always connects to **127.0.0.1**, because the host runs the server on the same PC. It sends `CREATE_ROOM {username, password?}`.
2. `on_create_room` validates the name and password **before** creating the room, so a bad request leaves no empty room behind. Usernames must be 1 to 24 printable characters (`rooms.MAX_USERNAME`).
3. `RoomManager.create_room` picks an unused 6-character code from `CODE_ALPHABET`. That is A-Z and 2-9 without 0, O, 1 and I, so a code read aloud is unambiguous. It uses `secrets.choice`, so codes cannot be guessed. Then it runs the `on_room_created` hooks: P3 attaches board, versions and assets, and P4 attaches chat.
4. `_seat` seats the creator. The room is empty, so the creator becomes **host**.

### 3.2 Join: `RoomManager.join` decision order (all under `room.lock`)

0. `room.closed` (deleted by `sweep` meanwhile): NO_ROOM.
1. A seat with this name exists **and** `token` matches (`secrets.compare_digest`): this is a **reconnect**. `resume_member` attaches the new session to the old seat, so name, role, colour and token are unchanged. A stale old connection still attached to the seat is closed.
2. The password is wrong: BAD_PASSWORD. A valid token in step 1 already proved membership, so a reconnect skips this step.
3. A seat with this name exists without a valid token: NAME_TAKEN.
4. Otherwise a new seat: host if the room has no members, else **editor**. The colour is the next of `config.USER_COLORS`, in join order.

Two clients racing for the same name cannot both win, because the check and the insert happen inside one `room.lock` block.

### 3.3 Password (salted SHA-256)

`Room.__init__` draws a 16-byte random salt (`secrets.token_bytes(16)`) **per room** and stores only `sha256(salt + password)` (`Room._hash`). `check_password` compares in constant time with `secrets.compare_digest`. The plain password is never stored on the server, and the hash lives in memory only, so it is gone after a restart (P3's restore keeps the board, not the password).

**Limitation, stated honestly:** the password travels **in clear text** inside the JOIN_ROOM payload, because there is no TLS. Anyone capturing LAN traffic (e.g. Wireshark on the same hub or Wi-Fi) can read it. The hash protects the stored value, not the transfer. The client also keeps the password in memory (`NetClient._join_payload`) to rejoin after a drop.

### 3.4 JOIN_OK and the join hooks (`presence._seat`)

Everything below runs inside **one** `with room.lock:` block:

```
client                               server (_seat, under room.lock)
JOIN_ROOM {room, username} ------->  RoomManager.join -> (member, rejoin)
                           <-------  JOIN_OK {room, token, role, color, users}
                                     rooms.manager.member_joined -> hooks in bootstrap order:
                           <-------    USER_LIST (to all)        presence._announce_join
other members              <-------    USER_JOINED {name,color}  (others only)
                           <-------    SNAPSHOT or SYNC_DELTA    sync.send_board_state (P3)
                           <-------    CHAT_HISTORY              chat (P4)
```

The lock is held from the seat to the last hook. No board operation can therefore be processed in between, and the snapshot the joiner gets matches exactly the seq that the next broadcast continues from. The token is sent **only** in JOIN_OK, to its owner. `Member.public()` leaves it out of USER_LIST.

On the client, `net_client._on_join_ok` fills `app.me` (username from the server-stamped `user` field, room, token, colour, role) and `app.users`.

### 3.5 Leave

- **Clean leave:** `NetClient.disconnect` queues `LEAVE` **before** the stop marker and waits up to 0.5 s for the sender thread, so LEAVE really leaves the machine before the app exits. Then `presence.on_leave` calls `member_left(..., remove=True, reason="leave")`. The seat is deleted, and the others get USER_LEFT `leave` plus USER_LIST. The TCP connection stays open, so the same client may CREATE or JOIN again.
- **Dropped connection** (no LEAVE): `ClientHandler._release_seat` (in the reader thread's `finally`) calls `member_left(..., remove=False, reason="timeout")`. The seat stays **offline** for `REJOIN_WINDOW_S` = 120 s. If a reconnect has already moved the seat to a new session (`member.session is not self`), nothing happens and no false USER_LEFT is sent.

### 3.6 KICK (`presence.on_kick`)

```
host            server                                   kicked client
KICK {user} --> permissions.can(stored role, "kick")?   (role from the server, not the message)
                under room.lock:
                  read target.session ONCE into `victim`
                  target.token = new random token        (old token can never resume)
                  victim.member = victim.room = None     (no second USER_LEFT "timeout")
                  member_left(remove=True, "kicked") --> others: USER_LEFT kicked, USER_LIST
                outside the lock:
                  victim.send(USER_LEFT about itself) ->  net_client._on_user_left:
                  victim.close_after_flush(2 s)            kicked_out(): no auto-reconnect,
                                                           back to the connect dialog
```

`close_after_flush` uses `shutdown(SHUT_WR)` instead of `close()`. If unread client frames are still in the server's receive buffer, `close()` makes the kernel send a **RST**, and on a RST the client's kernel may throw away the USER_LEFT it has not read yet (Windows always does). With `SHUT_WR`, the queued data goes out first, then a FIN. A client that never closes its side is cut off by a `threading.Timer` after 2 s. The kicked client recognises the kick because the USER_LEFT names **itself** with reason `kicked`. Without that message, a kick would look exactly like a dropped connection, and the client would reconnect.

---

## 4. Liveness and recovery

### 4.1 Heartbeat (`presence.Heartbeat`, one thread for the whole server)

Every `TICK_S` = 1 s, `Heartbeat.tick(now)`:

1. **Timeouts first.** Any connection with `now - c.last_rx > TIMEOUT_S` (15 s) is closed. `last_rx` is updated in `ClientHandler.run` for **every** frame, so cursor, chat and draw frames count as well as PONG. Closing wakes the reader thread, whose `finally` does the normal seat cleanup, so seat changes stay in one place.
2. **PING {t}** every `HEARTBEAT_S` = 5 s, to every connection, including ones that have not joined. A connection that never joins is therefore timed out too.
3. **STATS** every 2 s (section 6).
4. **`rooms.manager.sweep`**: drop offline seats older than 120 s, and delete rooms with nobody online for `ROOM_EMPTY_TTL_S` = 10 min.

The client answers PING with PONG **on the receiver thread** (`NetClient._receiver`), so GUI delays do not inflate the RTT. `presence.on_pong` computes `rtt = now - t` with the **server's own clock** (t is the server's timestamp echoed back), so the laptops' clocks do not need to agree. A forged or missing `t` is ignored unless it is a number with 0 ≤ rtt ≤ 15 s. The shown RTT is the moving average of the last 5 samples (`RTT_AVG_N`, a `deque(maxlen=5)`).

The client watches the server too. `NetClient._open_socket` sets `sock.settimeout(config.TIMEOUT_S)`. The server sends something at least every 2 s (STATS) and every 5 s (PING), so 15 s of silence means the link is dead. `recv` raises `socket.timeout`, and reconnecting starts.

### 4.2 Why TCP keepalive is not enough (comment block at the top of `presence.py`)

1. **Too slow by default.** Linux waits 2 h of idle time, then sends 9 probes 75 s apart (about 2 h 11 min). Windows also waits 2 h. Tuning needs per-OS options (`TCP_KEEPIDLE`/`TCP_KEEPINTVL`/`TCP_KEEPCNT` on Linux, the `SIO_KEEPALIVE_VALS` ioctl on Windows).
2. **It only proves that the peer's kernel is alive.** A client with a frozen GUI or a stuck thread still ACKs the probes.
3. **The application never sees it.** There is no RTT and nothing to show in the users or stats panels.

A Wi-Fi drop is exactly the case where nothing tells you: no FIN and no RST are ever sent, so a plain blocking `recv()` would wait forever on both ends.

**Backstop:** the server also sets a socket timeout of `SOCKET_TIMEOUT_S` = 15 + 5 = 20 s on every client socket (`server.py`). Once a dead client's send buffer is full, `sendall()` would otherwise block forever, and some sends happen while holding `room.lock` (section 5.2). With the timeout the send fails after 20 s, and the connection is closed.

### 4.3 Reconnect with token and last_seq

```
client B (Wi-Fi off)                                   server
  recv() silent 15 s -> socket.timeout                   no frame from B for 15 s -> close
  _on_lost: token kept -> emit reconnecting              member offline, USER_LEFT timeout to others
  _reconnect_worker: connect every 2 s (max 60 s)        seat kept 120 s (name, role, colour, token)
  connect OK -> JOIN_ROOM {room, username, token,
                           password?, last_seq}  ---->  join(): token matches -> resume_member
                                                <----  JOIN_OK (same role, colour, token)
                                                <----  USER_LIST, SYNC_DELTA {from, to, ops}
                                                         (or SNAPSHOT if the gap > MAX_DELTA = 500)
                                                <----  CHAT_HISTORY (client drops known msg_ids)
  emit reconnected -> toast "Reconnected, catching up..."
```

- Same name, colour and role: the seat (`Member`) outlives the TCP connection. The **token** (`secrets.token_hex(16)`, 128 bits) is the proof of ownership, and only that client ever received it.
- `last_seq` comes from `app.board.last_seq` (P3's BoardModel). The server answers with only the missing operations.
- If the user was kicked, `NetClient.kicked` is True and `_on_lost` never starts this loop.

### 4.4 Timeline from the constants (Wi-Fi off at t = 0)

| When | What | Where in the code |
|---|---|---|
| t = 0 | Wi-Fi off; no FIN/RST | |
| ~13–15 s | client notices: 15 s since the last frame (STATS every 2 s, so the last frame was ≤ 2 s before t = 0) | `_open_socket` timeout, `_receiver` |
| ~10–16 s | server notices: `last_rx` older than 15 s, checked every 1 s (the client's last PONG/CURSOR was up to 5 s before t = 0) | `Heartbeat.tick` |
| then | others see USER_LEFT `timeout`; users panel shows B offline | `_release_seat`, `_announce_leave` |
| every 2 s | client retries `connect` (each attempt up to 4 s, `CONNECT_TIMEOUT_S`) | `_reconnect_worker` |
| Wi-Fi back | next attempt succeeds → JOIN_ROOM with token → same seat | `_on_reconnect_done` |
| ~75 s | client gives up (60 s after it noticed) → "Could not reconnect ... within 60 s" | `RECONNECT_FOR_S` |
| ~130 s | server deletes the offline seat (120 s after it noticed) | `purge_expired` |

If the Wi-Fi returns **before** either side times out (under about 10 s), nothing visible happens. TCP retransmits the queued segments and the same connection simply continues.

**Known limitation (from reading the code, not yet seen in a test):** the client gives up at about 75 s but the server keeps the seat until about 130 s. In between, `Session > Reconnect` sends a fresh JOIN_ROOM **without** the token, because `NetClient` forgets it on giving up. The server answers **NAME_TAKEN** until the seat expires. Workaround: wait about one minute, or join with another name. A fix would be to keep the token after giving up, but that is not done before the deadline.

### 4.5 Test 1: local simulation of a vanished client (**measured**)

Setup: server on 127.0.0.1:5098. "Laptop A" was a socket client that created the room and answers PING. "Laptop B" was the **real** `NetClient` + `BoardModel` (Qt offscreen). B was frozen with `SIGSTOP` for 25 s. A frozen process sends no FIN and no RST and answers nothing, which looks the same to the server as a laptop whose Wi-Fi went off. While B was frozen, A drew 5 rectangles. B was then resumed with `SIGCONT`.

| Time after freeze | Observed |
|---|---|
| 0 s | B frozen |
| 14.6 s | server log `<client ... laptopB>: no frame for 15 s, timing out`; A receives `USER_LEFT {"name":"laptopB","color":"#3cb44b","reason":"timeout"}` and USER_LIST with `online: False` |
| 18.6 s | A draws 5 rectangles (seq 1–5); B misses them |
| 25.0 s | B resumed; its receiver reads EOF (the server had closed it), `reconnecting` |
| 25.0 s (+10 ms) | B connects, sends JOIN_ROOM with token and `last_seq = 0`; server log `laptopB rejoined room ... as editor`; A receives USER_JOINED and USER_LIST `online: True` |
| 25.0 s | B receives **SYNC_DELTA from 0 to 5, 5 ops**; B ends as `Me('laptopB', role='editor')` with colour `#3cb44b`, the same as before |

A first run, where A drew **before** the server timed B out, showed a TCP detail. The 5 operations were already in B's kernel receive buffer when the server closed the socket. On resume, B read them, then the FIN. It rejoined with `last_seq = 5` and got an empty SYNC_DELTA (5 to 5). Data already delivered to the kernel is not lost by a later close.

### 4.6 Test 2: two laptops, Wi-Fi switched off (**TO FILL**)

Setup: laptop A runs the server and a client and creates a room. Laptop B joins over Wi-Fi. While B's Wi-Fi is off, A draws a few strokes. Then B's Wi-Fi is switched back on.

| | Value |
|---|---|
| Network (Wi-Fi name / hotspot), OSes of A and B | TO FILL |
| Wi-Fi off duration | TO FILL |
| Time until A's users panel showed B offline (expected 10–16 s) | TO FILL |
| Time until B showed "Reconnecting..." (expected 13–15 s) | TO FILL |
| After Wi-Fi on: time until B was back (expected ≤ 2 s plus Wi-Fi association time) | TO FILL |
| B's name, colour and role after rejoin unchanged? | TO FILL |
| Strokes drawn by A while B was offline appeared on B? (SYNC_DELTA) | TO FILL |
| Chat sent while B was offline appeared on B? (CHAT_HISTORY) | TO FILL |

---

## 5. Concurrency

### 5.1 One thread per client: why

- **Simple blocking code:** `recv_msg` blocks, and only that client's thread waits. The handler code reads top to bottom with no callbacks or state machines, which is easy to explain and to check.
- **Isolation:** a slow, silent or malicious client blocks only its own thread. `ClientHandler.run` catches everything, so one bad client can only end its own connection.
- **Scale fits the use:** a lab room has tens of users, not thousands. Each thread costs a stack, of the order of the 8 MiB virtual reservation on Linux, most of it never touched. The CPython GIL is not a problem, because the threads spend almost all their time blocked in `recv` (I/O releases the GIL).
- **Alternatives** (`selectors`, `asyncio`) scale further but turn every handler into non-blocking code and would make the other owners' handlers harder to write. That is not worth it at this size.

The cost is that **shared room state is touched by many threads**, so it needs locks.

### 5.2 Which data needs which lock

| Lock | Kind | Protects | Notes |
|---|---|---|---|
| `Room.lock` | `RLock` | everything in a room: `members` and every `Member` field (`session`, `online`, `role`, `token`), the board and `seq` (P3), undo, versions, chat log writes (P4) | Every change of room state happens under it, so the room is serialised and the seq order is the truth (contract E1). An **R**Lock because a holder calls helpers that take it again (`_seat` → `join` → `add_member` → `next_color`; hooks → `broadcast`) |
| `RoomManager.lock` | `RLock` | only the `rooms` dict | Lock order is always `manager.lock` then `room.lock` (`sweep`). `join` never takes `manager.lock` while holding `room.lock`, so they cannot deadlock |
| per-socket send lock | `Lock` (`framing._lock_for`) | one socket's outgoing byte stream | frames never interleave |
| `framing._stats_lock` | `Lock` | `STATS` counters | `+=` on a dict entry is not atomic |
| `protocol._op_lock` | `Lock` | op_id counter | unique `<user>-<n>` |
| `ClientHandler._close_lock` | `Lock` | `closed` flag | the socket is closed exactly once, from any thread |
| `Server._clients_lock` | `Lock` | the set of live handlers | the heartbeat iterates a copy (`client_list`) |

What needs **no** lock, and why:
- `session.rtt_ms`, `last_rx`: one writer thread (the client's reader). A float assignment is atomic in CPython, and a reader seeing the old value for a moment is harmless.
- The CURSOR token bucket (`session.cursor_tokens`, `cursor_last`): only that client's reader thread touches it.
- `on_cursor` does not take `room.lock`, because it changes no room state. `broadcast` takes the lock itself to copy the targets.

**Send while holding the lock.** P3's `board_ops._process` and P4's chat broadcast **inside** `room.lock`. That is deliberate: two threads cannot interleave their broadcasts, so the order on the wire equals the seq order. Because `room.lock` is re-entrant, a broadcast from inside such a block still sends while the caller holds the lock. A client with a full TCP window can then stall that room for at most `SOCKET_TIMEOUT_S` = 20 s, after which its socket is closed. Callers that do not hold the lock (STATS, CURSOR, the end of KICK) send without it.

### 5.3 What happens if a client misbehaves

| Client does | Server reaction | Where |
|---|---|---|
| sends garbage bytes / invalid UTF-8 / invalid JSON / JSON that is not an object | `ProtocolError`, **this** connection closed, seat offline | `recv_msg`, `ClientHandler.run` |
| announces a frame > 8 MiB | refused before reading or allocating, connection closed | `recv_msg` |
| closes in the middle of a frame | `ProtocolError`, closed | `_recv_exact` |
| valid frame, but no `type` or a payload that is not an object | ERROR BAD_FORMAT, connection kept | `dispatch` |
| unknown type or type without handler | ERROR BAD_FORMAT | `dispatch` |
| anything except CREATE/JOIN/PONG before joining | ERROR NO_ROOM | `dispatch` (`_PRE_JOIN_TYPES`) |
| forges `user`, `seq`, `ts` or `room` | overwritten before the handler runs | `dispatch` |
| claims a role / KICKs as editor | the server uses its **stored** `member.role`: FORBIDDEN | `on_kick`, `permissions.can` |
| a message makes a handler crash | traceback logged, ERROR BAD_FORMAT, connection and server keep running | `dispatch` |
| a hook crashes | logged, the other hooks still run | `rooms._run_hooks` |
| floods CURSOR | token bucket (20/s, burst 5): the excess is dropped silently, with no ERROR (an answer per frame would add traffic) | `presence._cursor_allowed` |
| CURSOR with NaN, infinity, a bool or a huge value | BAD_FORMAT, or clamped to the board with one decimal | `_board_coord` |
| forged PONG `t` | ignored unless 0 ≤ rtt ≤ 15 s | `on_pong` |
| goes silent / freezes | closed after 15 s, seat offline | `Heartbeat.tick` |
| stops reading (full TCP window) | `sendall` times out after 20 s, closed | `SOCKET_TIMEOUT_S` |
| CREATE/JOIN twice on one connection | ERROR BAD_FORMAT "already in room" | `_already_seated` |
| guesses a token | 128-bit random, compared in constant time | `Member.token`, `compare_digest` |

### 5.4 The `Room.broadcast` check-then-use race (fixed in PR #30)

**Bug.** The old `broadcast` copied the member list under `room.lock`, released the lock, then for each member did:

```python
if m.session is not None:      # check  (lock already released)
    m.session.send(...)        # use: m.session read AGAIN
```

Between the check and the use, another thread could run `member_left()` and set `m.session = None`: the dying client's own reader thread, the heartbeat timing a client out, or a KICK. The broadcasting thread then crashed with `AttributeError: 'NoneType' object has no attribute 'send'`, and **every member after it in the list missed the message**. It was seen in the heartbeat's STATS broadcast.

**Fix** (`rooms.Room._snapshot_sessions`, `_safe_send`, `broadcast`, `send_to`):
1. Under `room.lock`, read `m.session` **exactly once** per member into a local list of `(name, session)` pairs. Another thread cannot change a local variable.
2. Release the lock, then send. `sendall` can block on a slow client, so the lock is not held for that, unless the caller already holds it (section 5.2).
3. A snapshotted session may close meanwhile. That is harmless: `ClientHandler.send` returns at once when `closed`, and `_safe_send` catches `OSError`, `AttributeError` and `ValueError` **per recipient**, so one dead client never stops the broadcast to the rest.

The same "read once under the lock" pattern is used in `presence.stats_payload`, `on_kick` (`victim = target.session`) and `ClientHandler.send` (`room = self.room`).

**Test:** `scripts/p1_test_broadcast_race.py` runs 6 fake members, 1000 online/offline flips in one thread, and broadcasts, `send_to` calls and STATS in others, with 2 % of the sends failing. Result: `flips: 1000, broadcasts: 599, send_to: 599, stats: 599` and `OK: zero exceptions` (measured).

---

## 6. Stats, live cursors and toasts

### 6.1 Message flow

```
STATS (every 2 s)
  Heartbeat.tick -> broadcast_stats: framing.stats_snapshot() + each online member's rtt_ms
     -> STATS to every room -> stats_panel._on_stats -> StatsModel.updated
        -> StatsPanel (RTT, coloured: green < 20 ms, amber < 100 ms, red above)
        -> UsersPanel.update_rtt (RTT next to each name)
  StatsPanel.sample (QTimer, 1 s): difference of this client's own framing.STATS
     -> msg/s and KB/s in/out, 60-sample live graph, "Copy stats" button

CURSOR (at most 20/s per client)
  canvas.cursor_moved(x, y) (every mouse move, can be 100+/s)
     -> CursorSender.on_moved: send now if >= 50 ms since the last send, else keep only the
        newest point and arm a one-shot PreciseTimer; flush() sends the latest point
        (so the resting position always arrives); skips duplicates and pauses while not in a room
     -> net.send (queue) -> server presence.on_cursor: permission, token bucket, clamp,
        rebuild {x, y} -> broadcast to the OTHERS with user = sender (no seq, no log)
     -> cursor_overlay._on_cursor -> target position
     -> CursorOverlay._tick (~60 fps): exponential smoothing (tau 0.06 s) toward the target,
        fade out after 3 s without updates, removed on USER_LEFT / offline in USER_LIST
        Positions are in board units and converted with canvas.board_to_screen at paint time,
        so every viewer's zoom and pan are respected.

Toasts (client/toasts.py)
  USER_JOINED / USER_LEFT (with reason) -> toast + generated chime (wave module, no files)
  NetClient.reconnecting / reconnected / disconnected -> toast (run_client.wire_status)
  ERROR outside the connect dialog -> friendly toast (connect_dialog._on_error)
  At most 3 on screen, 3 s each, click to dismiss. View > Mute sounds.
```

### 6.2 Traffic measurement (**measured**)

**Method.** Everything ran on one Linux laptop over loopback (127.0.0.1): one server and three clients in one room.

- `hostA` and `watcherB` were socket clients that answer PING and log every frame they receive.
- `drawer` ran the **real** client code (Qt offscreen): `NetClient`, `BoardModel`, `cursor_overlay.CursorSender` and `draw_sync.StrokeStreamer`.
- A `QTimer` fed `drawer` synthetic mouse moves at 125 Hz (the polling rate of an ordinary USB mouse) at 600 board units per second, in three 10 s phases:
  1. **idle**
  2. **hover**: mouse moving, no button pressed
  3. **draw**: mouse moving with pen strokes of 2 s each

The frame counts include the 4-byte header.

The rates are set by the client timers (50 ms cursor throttle, 40 ms stroke flush) and by the heartbeat, not by the network. They are therefore the same on a real LAN. Only the RTT differs: loopback RTT was 0.7–0.8 ms, and the LAN RTT is **TO FILL** from the stats panel in the lab.

Per client (`drawer`), 125 Hz mouse:

| Phase | Out msg/s | Out KB/s | In msg/s | In KB/s | What |
|---|---:|---:|---:|---:|---|
| idle | 0.2 | 0.02 | 0.7 | 0.14 | PONG out; PING (0.2/s) + STATS (0.5/s) in |
| hover | 19.9 | 2.35 | 0.7 | 0.15 | CURSOR 19.7/s × 120 B |
| draw | 45.6 | 8.22 | 26.4 | 6.46 | CURSOR 19.7/s + OP_STROKE_PART 25.7/s × 234 B; in: the echo of our own parts (251 B) |

What one **other** client (`watcherB`) receives:

| Phase | In msg/s | In KB/s | What |
|---|---:|---:|---|
| hover | 20.4 | 2.83 | relayed CURSOR 19.7/s × 139 B |
| draw | 46.1 | 9.14 | CURSOR + OP_STROKE_PART 25.7/s × 251 B |

Whole server, 3 clients (from the STATS counters):

| Phase | In msg/s | In KB/s | Out msg/s | Out KB/s |
|---|---:|---:|---:|---:|
| idle | 0.7 | 0.09 | 2.2 | 0.44 |
| hover (1 user moving) | 20.5 | 2.42 | 41.7 | 5.84 |
| draw (1 user drawing) | 46.3 | 8.35 | 119.3 | 24.95 |

Notes:
- The server's "draw" output adds up exactly: 2 × 19.7 cursor relays (to the 2 others) + 3 × 25.7 stroke-part echoes (to **all 3**, the sender included, per the echo rule) + PING + STATS = 118.6 msg/s.
- A relayed CURSOR is 139 B instead of 120 B because the server fills `user`, `room` and `ts`.
- **Throttles hold regardless of the mouse.** I repeated the run at 500 Hz (a gaming mouse). CURSOR stayed at 19.8/s and stroke parts at 25.6/s. Only the part size grew (306 B, about 10 points per part instead of 5). Half the points were dropped by P2's `MIN_DIST` = 1.5 units, because at 500 Hz they were only 1.2 units apart.
- Per stroke at 125 Hz: about 52 parts carry about 251 points over 2 s (`draw_sync` console line).

**Scaling (estimated, not measured):** with N users all moving their mouse, the server sends N × (N − 1) × 20 CURSOR frames per second. For 10 users that is 1800 msg/s × 139 B ≈ 244 KB/s, about 2 Mbit/s, which is fine for any Wi-Fi.

---

## 7. User guide (7.1 to 7.3)

### 7.1 Configure and start the server

1. Install Python 3.10 or later, then run `pip install -r requirements.txt`. PyQt5 is the only dependency.
2. On the host laptop, run `python run_server.py`. The output is:

   ```
   Whiteboard server listening on port 5050 (all interfaces)
   Clients type one of these addresses in Join > Server IP (port 5050):
     wlp0s20f3: 172.17.104.81   (Wi-Fi)   <- use this one
   Press Ctrl+C to stop.
   ```

   `run_server.print_addresses` lists every usable LAN interface. It hides loopback, link-local 169.254.x.x and virtual adapters (Docker, VirtualBox, VMware, WSL, VPN tunnels; `VIRTUAL_PREFIXES`) and marks the Wi-Fi address that has the default route.
3. Room data is written under `server_data/<ROOM>/`. Stop the server with Ctrl+C: `Server.stop` closes every client, and the clients see the drop and try to reconnect.

**Finding the LAN IP by hand**, if needed: on Linux `ip -4 addr` (or `hostname -I`), on Windows `ipconfig` (IPv4 Address of the Wi-Fi adapter), on macOS `ipconfig getifaddr en0`. The Host tab of the client also shows it in large text (`connect_dialog.lan_ip`).

**Port.** The default is TCP 5050 (`config.PORT`). `python run_server.py 6000` would listen on 6000, but the client's dialog always connects to `config.PORT` (`connect_dialog.py`, `_start`). In practice, **use 5050**. Another port works only if `common/config.py` is changed on every machine.

**Firewall.** The server PC must accept incoming TCP on 5050.
- Ubuntu (ufw): `sudo ufw allow 5050/tcp`, then check with `sudo ufw status`.
- Windows: on the first start, Windows Defender Firewall asks for Python. Tick **Private networks** and allow. If the prompt was dismissed, run this in an admin PowerShell: `New-NetFirewallRule -DisplayName "Whiteboard 5050" -Direction Inbound -Protocol TCP -LocalPort 5050 -Action Allow`. Also make sure the Wi-Fi network profile is **Private**, because Public blocks inbound connections.
- Firewall rules were **not tested on the lab machines yet** (TO FILL after the lab run).

### 7.2 Connect over the LAN

1. Run `python run_client.py`. The connect dialog opens.
2. **Join** tab: Server IP (the address the server marked "use this one"), Room code (6 characters, from the host), Username, Password (only if the room has one).
3. Errors are shown in plain language (`FRIENDLY_ERRORS`): wrong password, name already in use, no room with that code. If the server cannot be reached at all, the dialog adds: *"Check: same Wi-Fi as the host? Host's firewall allows TCP 5050? Try the host's other IP."*

### 7.3 Create or join a session

- **Host:** on the PC running the server, open the **Host** tab and enter a username and an optional password. The dialog connects to 127.0.0.1, creates the room, and shows the **room code** and **LAN IP** in large text with "Copy code and IP". Read these out to the others, then click "Open whiteboard". The creator is **host**. Everyone who joins is an **editor**, and the host can change roles (P4).
- **Status bar:** "Connected" (green), "Reconnecting..." (orange) or "Disconnected" (red). The window title shows the room code and your name.
- **Connection drops:** the client retries by itself every 2 s for 60 s and comes back with the same name, colour and role, and the board catches up. A toast says "Connection lost, reconnecting..." and then "Reconnected, catching up...".
- **Leave:** Session > Leave, or close the window (sends LEAVE). **Removed by the host:** a toast, then the connect dialog opens again.

> **Troubleshooting**
>
> | Symptom | Likely cause | Fix |
> |---|---|---|
> | "Could not reach ... (timed out)" | different network, Wi-Fi client isolation, or firewall | check both are on the same Wi-Fi; open TCP 5050; try `ping <host IP>` |
> | "Could not reach ... (Connection refused)" | server not running, or wrong IP/port | start `python run_server.py`; use the IP it printed |
> | Host tab: "Is the server running?" | the Host tab connects to 127.0.0.1 | start the server on **this** PC first |
> | **Campus Wi-Fi: both laptops online but cannot reach each other** (ping fails too) | **client isolation**: the access point blocks traffic between wireless clients | use a **phone hotspot** (or a lab switch/Ethernet). All laptops join the hotspot; the server prints its new IP |
> | **Server on Linux with Docker, Wi-Fi gives a 172.17.x.x address, other laptops time out** | **Docker's default bridge `docker0` is 172.17.0.0/16**: the same subnet as the Wi-Fi, so two routes compete and replies can go into `docker0` | check `ip route` for two `172.17.0.0/16` lines. Move Docker's bridge: `/etc/docker/daemon.json` = `{ "bip": "10.200.0.1/24" }`, then `sudo systemctl restart docker` (the setup on Sharwan's laptop: `docker0` is now 10.200.0.0/24 and the Wi-Fi is 172.17.0.0/16). Quick alternative: `sudo systemctl stop docker` |
> | Several IPs printed | VPN or second adapter | use the one marked "use this one"; the dialog hint says "try the host's other IP" |
> | NAME_TAKEN right after a long drop | old seat still kept (up to 2 min, section 4.4) | wait about a minute or pick another name |
> | "Your role does not allow that." | viewer role | ask the host to change your role |

---

## 8. Likely viva questions (short answers from the code)

1. **Why a 4-byte length prefix?**
   TCP is a byte stream without message boundaries. With the length first, the receiver knows exactly how many bytes make one message. There is no delimiter to escape, and the size is checked against `MAX_FRAME` (8 MiB) before any memory is allocated (`framing.recv_msg`). `!I` is big-endian, so all machines agree.

2. **What if `recv` returns only part of a frame?**
   That is normal for TCP. `framing._recv_exact` loops `recv_into` until exactly n bytes are in the buffer, for the 4-byte header and then for the body. A 0-byte `recv` means FIN: `None` at a frame boundary, `ProtocolError` in the middle. Sending uses `sendall` under a per-socket lock.

3. **How do you detect a client that vanished (Wi-Fi off, no FIN)?**
   Application heartbeat. The server PINGs every 5 s, and every received frame updates `last_rx`. `Heartbeat.tick` closes any connection silent for more than 15 s. Its reader thread then marks the seat offline and sends USER_LEFT `timeout`. The client does the same in the other direction with a 15 s socket timeout. Measured: detection 14.6 s after the client froze.

4. **Why not TCP keepalive?**
   The defaults take more than 2 hours, tuning is OS-specific, it only proves the peer's kernel is alive (a frozen app still ACKs), and it gives the application no RTT. Our PING/PONG is portable, detects in 15 s and measures RTT with one clock.

5. **How does a reconnecting user get the same name, colour and role?**
   The seat (`rooms.Member`) outlives the TCP connection: on a drop it is only marked offline for 120 s. JOIN_OK gave the client a secret 128-bit token. On reconnect, `NetClient` sends JOIN_ROOM with `token` and `last_seq`. `RoomManager.join` matches the token with `compare_digest` and `resume_member` attaches the new socket to the same `Member`. Then SYNC_DELTA (or SNAPSHOT) and CHAT_HISTORY fill the gap. Measured locally: 5 missed operations arrived as one SYNC_DELTA, with the same colour and role.

6. **Why one thread per client?**
   Blocking `recv` keeps the code simple, and a slow or bad client only blocks its own thread (everything is caught in `ClientHandler.run`). A lab has tens of clients, and the threads are almost always blocked in I/O, which releases the GIL. `selectors`/`asyncio` would scale further but make every handler non-blocking. The cost is shared state between threads, which is why every room has `room.lock`.

7. **Which data needs a lock?**
   All room state (members, each member's session/online/role/token, board and seq, chat) is protected by `room.lock`, an RLock because helpers re-enter it. The rooms dict has `RoomManager.lock`, always taken before `room.lock`, so there is no deadlock. Each socket has a send lock so frames never interleave. The traffic counters and the op_id counter have small locks. There is no lock for values with a single writer thread, such as `rtt_ms`, `last_rx` and the cursor token bucket.

8. **What was the bug fixed in PR #30?**
   A check-then-use race in `Room.broadcast`. It checked `m.session is not None` after releasing the lock, then read `m.session` again to send. Another thread could set it to None in between, causing `AttributeError`, and the remaining members missed the message. The fix reads each session once under the lock into a local snapshot, sends outside it, and catches errors per recipient. The stress test passes 1000 flips with zero exceptions.

9. **How does the server avoid trusting the client?**
   `dispatch` overwrites `user`, `room`, `seq` and `ts` before any handler runs. Roles come from the server's stored `Member.role`, and `permissions.can(role, action)` is checked before any change. Names, codes, coordinates and PONG times are validated. Messages before joining get NO_ROOM. CURSOR is rate-limited on the server too (token bucket, 20/s). The token is the only proof on reconnect, and a KICK replaces it.

10. **Why TCP and not UDP, and why `TCP_NODELAY`?**
    A whiteboard needs every operation, in order. With UDP we would have to rebuild retransmission and ordering ourselves. TCP gives a reliable ordered stream. The app adds `seq` only to repair gaps across **reconnects**, where TCP cannot help. `TCP_NODELAY` turns off Nagle's algorithm, which would hold small frames (CURSOR at 120 B, stroke parts at about 234 B) until earlier data is ACKed. With it off, they go out at once, which matters for live drawing.
