# P2 section: drawing, tools and stroke batching

Owner: P2 (Akshay). Code references are to `main` (contract rev 3).

Files of this section: `common/kinds_draw.py`, `client/canvas.py`, `client/toolbar.py`, `client/shortcuts.py`, `client/draw_sync.py`, `client/render_draw.py`, `client/tools/pen_tool.py`, `client/tools/shape_tools.py`, `client/tools/select_tool.py`, test `scripts/p2_render_test.py`.

---

## 1. Stroke batching: measurement

### What is compared

A freehand stroke is streamed as `OP_STROKE_PART` messages (contract C2) by `StrokeStreamer` in `client/draw_sync.py`:

- **Batching on** (`BATCHING = True`, the default): points are buffered and sent every `FLUSH_MS` = 40 ms by a `QTimer`, or earlier when the buffer holds `MAX_PART_POINTS` = 50 points. That is at most 25 parts per second per drawing user.
- **Batching off** (`BATCHING = False`): no timer. Every mouse point that is kept is sent at once as its own `OP_STROKE_PART`.

In both modes a point closer than `MIN_DIST` = 1.5 board units to the last kept point is dropped, so the two modes send the same points. Only how they are packed into messages differs.

### Method

- **Setup:** one server process and client A (draws) in a room, on one PC over loopback. A second watching client B does not change A's own counters, so it was left out. The numbers are read from P1's stats panel ("Latency and traffic" dock, `client/stats_panel.py`) on client A, which samples `framing.STATS` once a second. "Out" counts the frames A sends and "In" the frames A receives, including the server's echo of A's own parts (contract C2 echo rule).
- **Background traffic:** CURSOR (up to 20 per second while the mouse moves, `client/cursor_overlay.py`), PONG every 5 s, and STATS from the server every 2 s. To remove it, a **hover baseline** was measured: the same 5-second movement with no mouse button held.
- **Scribble:** a script drove the real `StrokeStreamer` and the real `CursorSender` with a simulated 125 Hz mouse (a typical polling rate): a zig-zag left and right across the page for 5 seconds at a steady speed (one sweep per second). Three runs per mode; the table gives the mean. The counters are `framing.STATS`, the same source the stats panel samples, read over the whole 5 s run. Because the mouse is simulated, repeat it by hand with the panel open to cross-check.
- **Per-stroke counts:** from the line `draw_sync.end()` prints on A's console, `[draw_sync] stroke <id>: <packets> packets, <points> points, <dropped> dropped (batching on|off)`.

### Results

Rates while drawing (stats panel on client A, mean of 3 runs of 5 s):

| Mode | Out msg/s | Out KB/s | In msg/s | In KB/s |
|---|---:|---:|---:|---:|
| Hover baseline (no button) | 17.1 | 1.95 | 0.7 | 0.13 |
| Batching on | 38.7 | 7.08 | 22.0 | 5.66 |
| Batching off | 141.2 | 23.03 | 124.7 | 23.88 |

Per 5-second stroke (console line on client A, mean of 3 runs):

| Mode | Stroke parts (messages) | Points sent | Points per message | Points dropped (< 1.5 units) |
|---|---:|---:|---:|---:|
| Batching on | 108 | 621 | 5.8 | 5 |
| Batching off | 622 | 621 | 1.0 | 5 |

Stroke traffic only (drawing minus hover baseline, "Out" side):

| Mode | Stroke msg/s | Stroke KB/s |
|---|---:|---:|
| Batching on | 21.6 | 5.13 |
| Batching off | 124.1 | 21.07 |
| Reduction with batching | 5.7 × fewer | 4.1 × less |

### Discussion

Without batching, every kept mouse point is its own message: 124 stroke messages per second at a 125 Hz mouse, one point per message. With batching the same 621 points travel in 108 messages, about 5.8 points each, which is 21.6 messages per second, just under the limit of 25 (one flush per 40 ms). The drop is 5.7 times in messages but only 4.1 times in bytes, because every message carries a fixed header and JSON envelope, and batching removes most of those but not the point data itself.

The saving grows with the mouse rate. The batched rate stays at 25 messages per second or less however fast the mouse reports, while the unbatched rate follows the mouse (up to 1000 per second for a gaming mouse). The "In" column shows the same effect: the server echoes every part back to A (contract C2), so unbatched drawing also receives about 125 messages per second.

Background traffic (about 17 CURSOR frames per second, PONG and STATS) is the same in both modes and is why the totals are higher than the stroke-only rows. Both modes send exactly the same 621 points; 5 were dropped by `MIN_DIST` where the zig-zag turns. The cost of batching is at most 40 ms of extra delay before a point leaves the client, which is below what a viewer can notice.

## 2. The page boundary

The page is `0..BOARD_W x 0..BOARD_H` (4000 x 3000). Nothing may be drawn outside it, and the rule is written in one place per side of the connection:

**Client (`client/canvas.py`).** Three helpers hold the rule: `clamp_point` (nearest point of the page), `clamp_rect` (push a box back inside, keeping its size) and `limit_to_page` (shorten a drag by one common factor, so a Shift 45 degree line or a square keeps its shape at the edge). The canvas uses them like this:

- a press that starts outside the page is not forwarded to the tool, so nothing happens;
- every point a tool receives is clamped, so a drag that leaves the page slides along the edge;
- objects are painted with a clip rectangle on the page, then the border is drawn on top, over a soft shadow;
- over the grey area a drawing tool gets the "not allowed" cursor;
- the select tool limits a move with `clamp_rect` (the object stops at the edge) and a resize with the clamped corner;
- the command bar refuses coordinates off the page with a message.

**Server side (`common/kinds_draw.py`).** The server never sees the clamping, so it must not trust it. `models.validate_obj` runs the `limits` of each kind, which now bound `x1/x2/y1/y2` and `x/y/w/h` to the page. A limit holds one field only, so `check()` also tests `x + w`, `y + h` and every stroke point; the client runs it before sending.
