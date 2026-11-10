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

- **Setup:** one server and two clients on the LAN, A (draws) and B (watches), in the same room. The numbers are read from P1's stats panel ("Latency and traffic" dock, `client/stats_panel.py`) on client A, which samples `framing.STATS` once a second. "Out" counts the frames A sends and "In" the frames A receives, including the server's echo of A's own parts (contract C2 echo rule).
- **Background traffic:** CURSOR (up to 20 per second while the mouse moves, `client/cursor_overlay.py`), PONG every 5 s, and STATS from the server every 2 s. To remove it, a **hover baseline** was measured: the same 5-second movement with no mouse button held.
- **Scribble:** pen tool, zoom fitted to the page. Zig-zag left and right across the page for 5 seconds at a steady speed, timed with a stopwatch. Three runs per mode; the table gives the mean.
- **Per-stroke counts:** from the line `draw_sync.end()` prints on A's console, `[draw_sync] stroke <id>: <packets> packets, <points> points, <dropped> dropped (batching on|off)`.

### Results

Rates while drawing (stats panel on client A, mean of 3 runs of 5 s):

| Mode | Out msg/s | Out KB/s | In msg/s | In KB/s |
|---|---:|---:|---:|---:|
| Hover baseline (no button) | – | – | – | – |
| Batching on | – | – | – | – |
| Batching off | – | – | – | – |

Per 5-second stroke (console line on client A, mean of 3 runs):

| Mode | Stroke parts (messages) | Points sent | Points per message | Points dropped (< 1.5 units) |
|---|---:|---:|---:|---:|
| Batching on | – | – | – | – |
| Batching off | – | – | – | – |

Stroke traffic only (drawing minus hover baseline, "Out" side):

| Mode | Stroke msg/s | Stroke KB/s |
|---|---:|---:|
| Batching on | – | – |
| Batching off | – | – |
| Reduction with batching | – × fewer | – × less |

### Discussion

_To be written from the numbers above._
