"""kinds_image.py: KINDS registry for image objects, and the constants of the image transfer (contract C3 and D).

Contract section D:   image   {x, y, w, h, asset_id}   (x, y, w, h in board units; asset_id names the picture)

The picture itself is NOT inside the board object. It travels once, as an "asset", with the messages
IMG_BEGIN, IMG_CHUNK x n, IMG_END (client to server), is answered with IMG_OK, and is fetched by the other
clients with ASSET_GET when they first need to draw it. The board object only carries the asset_id, so a
SNAPSHOT or an undo never has to move megabytes.

asset_id is CONTENT ADDRESSED: it is the sha256 (64 lowercase hex characters) of the picture's bytes.
That choice makes three things simple and safe:
  * the server can check the id against the bytes it received (an id can never name different pictures,
    so nobody can replace somebody else's picture by reusing its id);
  * uploading the same picture twice stores it once;
  * an id is always a plain hex string, so a client may use it as a file name in its disk cache
    (is_asset_id() is checked before every use as a path).

WHY THE PICTURE IS SENT IN CHUNKS even though a frame may hold 8 MiB (config.MAX_FRAME) and TCP would
carry one big frame just fine:
  1. One TCP connection carries EVERYTHING of a client: strokes, cursors, chat and the PING/PONG
     heartbeat. framing.send_msg holds a per-socket lock while it writes a frame, so a single 6.7 MB
     frame (a 5 MB picture as base64) blocks every other message on that socket until it is out. On a slow
     Wi-Fi that is several seconds; the heartbeat would miss its deadline (config.TIMEOUT_S, 15 s) and the
     uploader could be declared offline. With 24 KB chunks the other messages are interleaved between
     chunks and nobody notices the upload.
  2. The same lock is held by the SERVER when it broadcasts or streams to a client (ASSET_GET), so one
     huge frame would stall chat and drawing for that client too.
  3. The receiver must hold a whole frame in memory and parse it as ONE JSON text before it knows what it
     is. With chunks the server judges the upload from the small IMG_BEGIN (declared size above the limit:
     refused before a single picture byte arrives) and aborts at the first wrong chunk.
  4. Progress and cancelling: the sender can show "chunk 12 of 214" and stop between chunks; a single
     frame is all or nothing.
  5. Nobody (reader thread, GUI thread) has to encode or decode one giant string in one go.
The price is a few percent more bytes (JSON envelope per chunk) and a little code to reassemble: both small.
"""
import re

NUMBER = (int, float)     # JSON does not distinguish 1 from 1.0

# --- The transfer (contract C3) ------------------------------------------------------------------------
MAX_IMAGE_BYTES = 5 * 1024 * 1024       # contract: 5 MB per image (the encoded picture)
CHUNK_BYTES = 24 * 1024                 # contract: at most 24 KB of raw bytes per IMG_CHUNK
MAX_SIDE = 1600                         # contract: the client downscales to 1600 px on the longest side
MIMES = ("image/png", "image/jpeg")     # the two encodings the client produces
MAX_NAME = 120                          # longest file name kept (it is only a label)

# --- The KINDS registry (contract section D, limits as in contract rev 3) ------------------------------------

LIMITS = {
    "w": [1, 4000],             # a picture is at least 1 unit and at most as big as the board
    "h": [1, 4000],
    "asset_id": [64, 64],       # strings: the LENGTH must be in this range (a sha256 in hex)
}

KINDS = {
    "image": {
        "required": {"x": NUMBER, "y": NUMBER, "w": NUMBER, "h": NUMBER, "asset_id": str},
        "defaults": {},
        "limits": {field: list(bounds) for field, bounds in LIMITS.items()},
    },
}

_ASSET_ID = re.compile(r"[0-9a-f]{64}\Z")


def is_asset_id(value):
    """True for a well-formed asset id: exactly 64 lowercase hex characters (a sha256).

    The server stores only such ids, and the client checks it before it uses
    an id as a file name, so an id taken from the network can never contain
    a path ("../../x") or any other surprise.
    """
    return isinstance(value, str) and _ASSET_ID.match(value) is not None


def chunk_count(size):
    """Number of IMG_CHUNK messages for a picture of `size` bytes (the last one may be shorter)."""
    return -(-size // CHUNK_BYTES)


def mime_matches(mime, data):
    """True if the first bytes of `data` are those of a PNG (image/png) or JPEG (image/jpeg) file.

    The server uses this so that it never stores or serves something that
    merely CLAIMS to be a picture.
    """
    if mime == "image/png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if mime == "image/jpeg":
        return data[:3] == b"\xff\xd8\xff"
    return False


def get(obj, field):
    """Return obj[field], or the kind's default (there are none for images) when it is missing."""
    if field in obj:
        return obj[field]
    return KINDS.get(obj.get("kind"), {}).get("defaults", {}).get(field)
