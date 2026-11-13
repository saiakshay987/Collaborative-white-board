# -*- mode: python ; coding: utf-8 -*-
"""WhiteboardServer.spec: PyInstaller recipe for the server executable.

Console app (it prints the IP address to give to friends), one single file,
entry point run_server.py. Build it with scripts/build.sh or scripts/build.bat,
which run:  pyinstaller --clean packaging/WhiteboardServer.spec

hiddenimports: the server registers its handlers by importing modules
(server/bootstrap.py). Those are plain static imports, which PyInstaller
already follows, but listing every one of them here as well makes the build
independent of how the import is written: the list is read from
server/bootstrap.py itself (so it can never go out of date) plus every
module in common/.

The server uses only the standard library (socket, threading, json), so PyQt5
and tkinter are excluded: the result is a few MB instead of ~40 MB.
"""

import ast
import glob
import os

# SPECPATH is set by PyInstaller: the folder of this .spec file (packaging/).
ROOT = os.path.dirname(os.path.abspath(SPECPATH))


def bootstrap_modules(path):
    """Every module imported by a bootstrap file, e.g. 'server.rooms', found with ast (no import is run)."""
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            # "from server import rooms" -> server.rooms ; "from client.tools import pen_tool" -> client.tools.pen_tool
            found += ["%s.%s" % (node.module, alias.name) for alias in node.names]
        elif isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
    return found


def package_modules(package):
    """Every module of a package folder, e.g. common.config, common.framing, ..."""
    files = sorted(glob.glob(os.path.join(ROOT, package, "*.py")))
    return [package] + ["%s.%s" % (package, os.path.splitext(os.path.basename(f))[0])
                        for f in files if not f.endswith("__init__.py")]


HIDDEN = sorted(set(bootstrap_modules(os.path.join(ROOT, "server", "bootstrap.py"))
                    + ["server.bootstrap", "server.server"]
                    + package_modules("common")))
print("WhiteboardServer hiddenimports:", ", ".join(HIDDEN))

a = Analysis(
    [os.path.join(ROOT, "run_server.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=["PyQt5", "tkinter", "_tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# One-file: binaries and data are packed into the executable itself.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WhiteboardServer",
    debug=False,
    strip=False,
    upx=False,          # UPX off: smaller gain than antivirus false positives cost
    console=True,       # the server prints its IP address and logs to the console
    runtime_tmpdir=None,
)
