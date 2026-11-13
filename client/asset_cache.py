"""asset_cache.py: Client-side cache of the pictures of image objects, fetched from the server with ASSET_GET (contract C3).

An image object only carries an asset_id (see common/kinds_image.py). The renderer asks this cache for the
picture every time it paints:

    cache.get_image(asset_id)  ->  a QImage, or None

and the cache finds it in the cheapest place first:

    1. MEMORY     an LRU of decoded images (MAX_MEMORY_IMAGES): painting is a dictionary lookup
    2. DISK       <cache dir>/<asset_id>.bin, the encoded PNG/JPEG. Kept between runs, so a picture is downloaded
                  once per machine, not once per session. The asset id is the sha256 of the bytes, so a file is
                  CHECKED against its own name when it is read: a damaged or replaced file is deleted and ignored
    3. SERVER     one ASSET_GET {asset_id}; the answer arrives as IMG_BEGIN, IMG_CHUNK x n, IMG_END, is reassembled
                  and verified (size, sha256, file type) here, then stored on disk and in memory, and the canvas is
                  repainted

"Once": an asset is requested a single time. It is asked for again only if nothing came back for RETRY_S seconds
(the answer was lost with a dropped connection) and the renderer still needs it, or after a reconnect
(JOIN_OK clears the requests). Meanwhile get_image returns None and the renderer draws its placeholder; state()
tells it whether to say 'loading' or 'unavailable' (the server said it does not have the picture, or the bytes
were damaged).

Safety: an id comes from the network, and a file name is made of it, so common.kinds_image.is_asset_id (exactly
64 lowercase hex characters) is checked before ANY use as a path. A download is only accepted for an asset that
was asked for, never above MAX_IMAGE_BYTES, in order, and every chunk must have the announced length.

Everything here runs on the GUI thread (messages arrive through registry.deliver_message); the disk reads are of
files of at most 5 MB. Nothing blocks on the network: requests are NetClient.send calls.
"""
import base64
import hashlib
import os
import tempfile
import time
from collections import OrderedDict, deque

from PyQt5.QtCore import QObject, QStandardPaths, pyqtSignal
from PyQt5.QtGui import QImage

from common import kinds_image, protocol
from client.registry import on_message

MAX_MEMORY_IMAGES = 24      # decoded images kept (a 1600 x 1600 image is about 10 MB decoded)
RETRY_S = 20.0              # ask again when nothing came back for this long
CACHE_FOLDER = "whiteboard-assets"

READY, LOADING, FAILED, INVALID = "ready", "loading", "failed", "invalid"     # what state() returns


def default_cache_dir():
    """The folder of the disk cache: the user's cache location (Qt's choice per OS) plus CACHE_FOLDER."""
    base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation) or os.path.join(tempfile.gettempdir(), "whiteboard")
    return os.path.join(base, CACHE_FOLDER)


class AssetCache(QObject):
    """Memory + disk cache of pictures, and the client side of ASSET_GET (see the module docstring).

    loaded     signal(asset_id): a picture became available
    app        the AppState, set by the message handlers below (needed to send ASSET_GET and to repaint)
    clock      replaceable time source, so tests need no sleeping
    """

    loaded = pyqtSignal(str)

    def __init__(self, cache_dir=None, parent=None):
        """Create an empty cache; the disk folder is created when the first picture is written."""
        super().__init__(parent)
        self.cache_dir = cache_dir
        self.app = None
        self.clock = time.monotonic
        self.images = OrderedDict()     # asset_id -> QImage, least recently used first
        self.requested = {}             # asset_id -> clock() of the last ASSET_GET
        self.outstanding = deque()      # asset ids asked for and not answered yet, oldest first (the server answers in order)
        self.incoming = {}              # asset_id -> {'meta', 'buf', 'next'} of a download in progress
        self.failed = set()             # ids the server does not have, or whose bytes were bad
        self._not_on_disk = set()       # ids already looked up on disk without success (no stat() at every paint)

    # ------------------------------------------------------------------ disk
    def directory(self):
        """The disk cache folder (default_cache_dir() unless one was given)."""
        if self.cache_dir is None:
            self.cache_dir = default_cache_dir()
        return self.cache_dir

    def _path(self, asset_id):
        """File of an asset, or None when the id is not a well-formed asset id (never build a path from anything else)."""
        if not kinds_image.is_asset_id(asset_id):
            return None
        return os.path.join(self.directory(), asset_id + ".bin")

    def _read_disk(self, asset_id):
        """The verified bytes of an asset from the disk cache, or None. A bad file is deleted."""
        path = self._path(asset_id)
        if path is None or asset_id in self._not_on_disk:
            return None
        try:
            with open(path, "rb") as f:
                data = f.read(kinds_image.MAX_IMAGE_BYTES + 1)
        except OSError:
            self._not_on_disk.add(asset_id)
            return None
        if len(data) > kinds_image.MAX_IMAGE_BYTES or hashlib.sha256(data).hexdigest() != asset_id:
            try:
                os.remove(path)
            except OSError:
                pass
            self._not_on_disk.add(asset_id)
            return None
        return data

    def _write_disk(self, asset_id, data):
        """Store the bytes in the disk cache (atomic: temp file, then rename). A failure only means no disk cache."""
        path = self._path(asset_id)
        if path is None:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            pass

    # ---------------------------------------------------------------- memory
    def _remember(self, asset_id, image):
        """Put a decoded image in the memory LRU, dropping the least recently used ones."""
        self.images[asset_id] = image
        self.images.move_to_end(asset_id)
        while len(self.images) > MAX_MEMORY_IMAGES:
            self.images.popitem(last=False)

    def store(self, asset_id, data):
        """Add a picture whose bytes we already have (my own upload, or a finished download). Returns True if it is usable.

        The bytes must hash to the id and decode to a real image; then they go
        to the disk cache and the memory LRU.
        """
        if not kinds_image.is_asset_id(asset_id) or hashlib.sha256(data).hexdigest() != asset_id:
            return False
        image = QImage.fromData(data)
        if image.isNull():
            return False
        self._write_disk(asset_id, data)
        self._not_on_disk.discard(asset_id)
        self._remember(asset_id, image)
        self.failed.discard(asset_id)
        return True

    # --------------------------------------------------------------- reading
    def get_image(self, asset_id, request=True):
        """The QImage of an asset, or None while it is not available.

        memory, then disk, then (if `request`) one ASSET_GET to the server. An
        export passes request=False: it must never start a download.
        """
        if not kinds_image.is_asset_id(asset_id):
            return None                                     # also keeps an unhashable value out of the dictionary lookups
        image = self.images.get(asset_id)
        if image is not None:
            self.images.move_to_end(asset_id)
            return image
        data = self._read_disk(asset_id)
        if data is not None:
            image = QImage.fromData(data)
            if not image.isNull():
                self._remember(asset_id, image)
                return image
        if request:
            self.request(asset_id)
        return None

    def state(self, asset_id):
        """READY, LOADING, FAILED or INVALID (not an asset id), for the caption of a placeholder."""
        if not kinds_image.is_asset_id(asset_id):
            return INVALID
        if asset_id in self.images:
            return READY
        return FAILED if asset_id in self.failed else LOADING

    # -------------------------------------------------------------- download
    def request(self, asset_id):
        """Send ASSET_GET for an asset, at most once per RETRY_S. Returns True if a request was sent."""
        if self.app is None or self.app.net is None or not kinds_image.is_asset_id(asset_id):
            return False
        now = self.clock()
        last = self.requested.get(asset_id)
        if last is not None and now - last < RETRY_S:
            return False
        if not self.app.net.send(protocol.ASSET_GET, {"asset_id": asset_id}):
            return False                                    # not connected: try again at a later paint
        self.requested[asset_id] = now
        if asset_id in self.outstanding:
            self.outstanding.remove(asset_id)
        self.outstanding.append(asset_id)
        self.incoming.pop(asset_id, None)                   # a retry starts the transfer from the beginning
        return True

    def _fail(self, asset_id):
        """The download of an asset is over and unusable: forget its state and remember that it failed."""
        self.incoming.pop(asset_id, None)
        if asset_id in self.outstanding:
            self.outstanding.remove(asset_id)
        self.failed.add(asset_id)
        self._repaint()

    def on_begin(self, p):
        """IMG_BEGIN from the server: open the download of an asset we asked for."""
        asset_id = p.get("asset_id")
        if not kinds_image.is_asset_id(asset_id) or asset_id not in self.requested:
            return                                          # not something we asked for: ignore it
        size, chunks = p.get("size"), p.get("chunks")
        if (p.get("mime") not in kinds_image.MIMES
                or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= kinds_image.MAX_IMAGE_BYTES
                or isinstance(chunks, bool) or chunks != kinds_image.chunk_count(size)):
            self._fail(asset_id)                            # we asked, but the answer makes no sense
            return
        self.incoming[asset_id] = {"mime": p["mime"], "size": size, "buf": bytearray(), "next": 0}

    def on_chunk(self, p):
        """IMG_CHUNK from the server: append the next piece of a download in progress."""
        asset_id, idx, text = p.get("asset_id"), p.get("idx"), p.get("data")
        down = self.incoming.get(asset_id) if kinds_image.is_asset_id(asset_id) else None
        if down is None:
            return                                          # not a download we know (a retry superseded it)
        try:
            raw = base64.b64decode(text, validate=True) if isinstance(text, str) else None
        except ValueError:
            raw = None
        last = idx == kinds_image.chunk_count(down["size"]) - 1
        want = down["size"] - kinds_image.CHUNK_BYTES * idx if last else kinds_image.CHUNK_BYTES
        if isinstance(idx, bool) or idx != down["next"] or raw is None or len(raw) != want:
            self._fail(asset_id)
            return
        down["buf"] += raw
        down["next"] += 1

    def on_end(self, p):
        """IMG_END from the server: verify size, sha256 and file type, then store the picture and repaint."""
        asset_id = p.get("asset_id")
        down = self.incoming.pop(asset_id, None) if kinds_image.is_asset_id(asset_id) else None
        if down is None:
            return
        data = bytes(down["buf"])
        if (len(data) != down["size"] or p.get("sha256") != asset_id or hashlib.sha256(data).hexdigest() != asset_id
                or not kinds_image.mime_matches(down["mime"], data) or not self.store(asset_id, data)):
            self._fail(asset_id)
            return
        if asset_id in self.outstanding:
            self.outstanding.remove(asset_id)
        self.loaded.emit(asset_id)
        self._repaint()

    def on_error(self, p):
        """ERROR for an ASSET_GET: the server does not have the oldest picture we are waiting for.

        The ERROR payload carries no asset id (contract B), but the server
        answers requests in the order they were sent on this connection, so
        it belongs to the oldest outstanding one.
        """
        if p.get("ref_type") == protocol.ASSET_GET and self.outstanding:
            self._fail(self.outstanding[0])

    def reset(self):
        """JOIN_OK (first join or reconnect): drop what belonged to the old connection so pictures are asked for again."""
        self.requested.clear()
        self.outstanding.clear()
        self.incoming.clear()
        self.failed.clear()

    def _repaint(self):
        """Ask the canvas to paint again (a picture arrived, or turned out to be unavailable)."""
        canvas = getattr(self.app, "canvas", None) if self.app is not None else None
        if canvas is not None:
            canvas.update()


# The one cache of the client process (a plain QObject: it may exist before the QApplication does).
cache = AssetCache()


@on_message(protocol.JOIN_OK)
def _on_join_ok(app, msg):
    """JOIN_OK: remember the app and forget the requests of the previous connection."""
    cache.app = app
    cache.reset()


@on_message(protocol.IMG_BEGIN)
def _on_img_begin(app, msg):
    """IMG_BEGIN (a download we asked for)."""
    cache.app = app
    cache.on_begin(msg.get("payload") or {})


@on_message(protocol.IMG_CHUNK)
def _on_img_chunk(app, msg):
    """IMG_CHUNK (a download we asked for)."""
    cache.on_chunk(msg.get("payload") or {})


@on_message(protocol.IMG_END)
def _on_img_end(app, msg):
    """IMG_END (a download we asked for)."""
    cache.on_end(msg.get("payload") or {})


@on_message(protocol.ERROR)
def _on_error(app, msg):
    """ERROR: only the ones about ASSET_GET matter here (others are shown by the generic handler)."""
    cache.on_error(msg.get("payload") or {})
