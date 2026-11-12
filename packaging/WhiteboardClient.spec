# OWNER: P1
# -*- mode: python ; coding: utf-8 -*-
"""WhiteboardClient.spec: PyInstaller recipe for the client executable.

Windowed app (no console window on Windows), one single file, entry point
run_client.py. Build it with scripts/build.sh or scripts/build.bat, which run:
    pyinstaller --clean packaging/WhiteboardClient.spec

WhiteboardClient-console.spec runs this same file with CONSOLE = True, which
gives a client that keeps its console window open, to read print() output and
tracebacks while debugging.

What goes in:
  * hiddenimports: every module that client/bootstrap.py imports (read from
    the file itself with ast, so the list can never go out of date), plus
    run_client.py's own imports and every module in common/.
  * Qt platform plugins (PyInstaller's PyQt5 hook adds them: windows / xcb,
    wayland, offscreen, ...). The build fails if they are missing.
  * imageformats: only jpeg, gif and webp. PNG and BMP are built into QtGui;
    the image tool opens *.png *.jpg *.jpeg *.bmp *.gif *.webp. The other
    format plugins (svg, tiff, icns, tga, wbmp, ...) are dropped.
  * QtMultimedia + its audio plugins: client/toasts.py plays the join/leave
    sounds with QSoundEffect. It is optional there (if the import or the
    audio fails, toasts uses QApplication.beep()), so the client also runs
    without them. The media-player back-ends (GStreamer etc.) are dropped.
  * Qt plugins we never use (webgl/eglfs/vnc platforms, bearer, evdev) and
    the libraries only they need (Qt5Quick, Qt5Qml, ...) are dropped; see
    DROP_* below.
  * Unneeded Qt modules (QtWebEngine*, QtQml, QtQuick, QtSql, QtTest, Qt3D*,
    ...) and tkinter are excluded to keep the file small. UPX is off.
"""

import ast
import glob
import os

# Set to True by WhiteboardClient-console.spec before it runs this file.
CONSOLE = globals().get("CONSOLE", False)
NAME = "WhiteboardClient-console" if CONSOLE else "WhiteboardClient"

# SPECPATH is set by PyInstaller: the folder of the .spec file (packaging/).
ROOT = os.path.dirname(os.path.abspath(SPECPATH))


def bootstrap_modules(path):
    """Every module imported by a Python file, e.g. 'client.tools.pen_tool', found with ast (nothing is run)."""
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found += ["%s.%s" % (node.module, alias.name) for alias in node.names]
        elif isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
    # Keep our own modules; "from PyQt5.QtCore import QTimer" is a class, not a module.
    return [m for m in found if m.split(".")[0] in ("client", "common")]


def package_modules(package):
    """Every module of a package folder, e.g. common.config, common.framing, ..."""
    files = sorted(glob.glob(os.path.join(ROOT, package, "*.py")))
    return [package] + ["%s.%s" % (package, os.path.splitext(os.path.basename(f))[0])
                        for f in files if not f.endswith("__init__.py")]


HIDDEN = sorted(set(bootstrap_modules(os.path.join(ROOT, "client", "bootstrap.py"))
                    + bootstrap_modules(os.path.join(ROOT, "run_client.py"))
                    + ["client.bootstrap"]
                    + package_modules("common")
                    + ["PyQt5.sip", "PyQt5.QtMultimedia"]))


def is_module(name):
    """True for a real module or package of ours (a .py file or a folder), False for a class name.

    run_client.py has both kinds: "from client import toasts" names a module,
    "from client.app_state import AppState" names a class.
    """
    if not name.startswith(("client", "common")):
        return True                                   # PyQt5.* entries added above
    path = os.path.join(ROOT, *name.split("."))
    return os.path.isdir(path) or os.path.isfile(path + ".py")


HIDDEN = [m for m in HIDDEN if is_module(m)]
print("%s hiddenimports: %s" % (NAME, ", ".join(HIDDEN)))

EXCLUDES = [
    "tkinter", "_tkinter",
    "PyQt5.QtWebEngine", "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebChannel",
    "PyQt5.QtWebSockets", "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtQuickWidgets", "PyQt5.QtSql",
    "PyQt5.QtTest", "PyQt5.Qt3DCore", "PyQt5.Qt3DRender", "PyQt5.Qt3DInput", "PyQt5.Qt3DLogic",
    "PyQt5.Qt3DAnimation", "PyQt5.Qt3DExtras", "PyQt5.QtDesigner", "PyQt5.QtHelp", "PyQt5.QtBluetooth",
    "PyQt5.QtNfc", "PyQt5.QtLocation", "PyQt5.QtPositioning", "PyQt5.QtSensors", "PyQt5.QtSerialPort",
    "PyQt5.QtXmlPatterns", "PyQt5.QtRemoteObjects", "PyQt5.QtTextToSpeech", "PyQt5.uic",
]

a = Analysis(
    [os.path.join(ROOT, "run_client.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

# --- Qt plugins: keep only the image formats we need, check the platform plugins ---

KEEP_IMAGEFORMATS = ("qjpeg", "qgif", "qwebp")   # png and bmp are built into QtGui

# Platform plugins never used on a lab laptop. qwebgl (streams the window to a
# browser) is the reason Qt5Quick + Qt5Qml (13 MB) were bundled; eglfs, linuxfb,
# vnc and minimalegl are for embedded boards. Kept: windows, xcb (X11),
# wayland*, offscreen (smoke test) and minimal.
DROP_PLATFORMS = ("qwebgl", "qeglfs", "qlinuxfb", "qvnc", "qminimalegl")

# Whole plugin folders the client never uses:
#   mediaservice, playlistformats: QMediaPlayer / camera back-ends (GStreamer on
#       Linux, DirectShow / WMF on Windows). QSoundEffect does not use them: it
#       plays through the audio plugins (and PulseAudio inside Qt5Multimedia on Linux).
#   bearer: Qt's network-session manager; our networking is Python sockets.
#   generic: evdev mouse/keyboard/touch input, only for eglfs / linuxfb.
DROP_PLUGIN_FOLDERS = ("mediaservice", "playlistformats", "bearer", "generic")

# Libraries that only the dropped plugins needed (matched on the file name,
# Windows: Qt5Quick.dll, Linux: libQt5Quick.so.5, ...).
DROP_LIBS = ("qt5quick", "qt5qml", "qt5eglfs", "qt5multimediagsttools", "qt5multimediaquick",
             "qt5multimediawidgets", "libgst")


def plugin_parts(dest):
    """('imageformats', 'libqjpeg.so') for '.../plugins/imageformats/libqjpeg.so', else None."""
    parts = dest.replace("\\", "/").split("/")
    if "plugins" in parts and len(parts) > parts.index("plugins") + 2:
        i = parts.index("plugins")
        return parts[i + 1], parts[-1]
    return None


def keep_binary(entry):
    """False for the Qt plugins and libraries listed above, True for everything else."""
    found = plugin_parts(entry[0])
    if found:
        folder, name = found
        if folder == "imageformats":
            return any(k in name for k in KEEP_IMAGEFORMATS)
        if folder == "platforms":
            return not any(k in name for k in DROP_PLATFORMS)
        return folder not in DROP_PLUGIN_FOLDERS
    base = os.path.basename(entry[0].replace("\\", "/")).lower()
    return not any(base.startswith(p) or base.startswith("lib" + p) for p in DROP_LIBS)


a.binaries = [b for b in a.binaries if keep_binary(b)]
# On Linux PyInstaller also adds top-level SYMLINK aliases (libQt5Qml.so.5 ->
# PyQt5/Qt5/lib/libQt5Qml.so.5); drop those whose target was just removed, so
# the bundle has no broken links.
_kept = {b[0] for b in a.binaries if b[2] != "SYMLINK"} | {d[0] for d in a.datas}
a.binaries = [b for b in a.binaries if b[2] != "SYMLINK" or b[1] in _kept]
a.datas = [d for d in a.datas if d[2] != "SYMLINK" or d[1] in _kept]
categories = {}
for b in a.binaries:
    found = plugin_parts(b[0])
    if found:
        categories.setdefault(found[0], []).append(found[1])
for cat in sorted(categories):
    print("%s Qt plugins/%s: %s" % (NAME, cat, ", ".join(sorted(categories[cat]))))
if "platforms" not in categories:
    raise SystemExit("ERROR: no Qt platform plugins collected; the client would not start.")
missing = [k for k in KEEP_IMAGEFORMATS if not any(k in f for f in categories.get("imageformats", []))]
if missing:
    raise SystemExit("ERROR: image format plugins missing: %s" % ", ".join(missing))
if "audio" not in categories:
    print("WARNING: no QtMultimedia audio plugins collected; join/leave sounds will fall back to a beep.")

pyz = PYZ(a.pure)

# One-file: binaries and data are packed into the executable itself.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    debug=False,
    strip=False,
    upx=False,          # UPX off: smaller gain than antivirus false positives cost
    console=CONSOLE,    # False: a normal windowed app; True: keeps a console for debugging
    runtime_tmpdir=None,
)
