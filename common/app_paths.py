"""app_paths.py: Where the program may keep files that must survive a restart.

A PyInstaller one-file executable unpacks its bundled modules into a fresh
temporary folder (sys._MEIPASS) on every start and deletes it on exit, so
anything written relative to __file__ in a frozen build is lost. Files that
must persist (the server's rooms in server_data/) therefore go next to the
executable itself. When running from source, the same files go in the repo
root, which is where `python run_server.py` has always put them.

    base_dir()          -> folder of the .exe (frozen) or the repo root (source)
    data_dir("name")    -> <base_dir>/name, created if missing
"""

import os
import sys


def base_dir():
    """Folder that holds persistent files: the executable's folder when frozen, else the repo root.

    sys.frozen is set by PyInstaller in a built executable; sys.executable is
    then the .exe itself (not a Python interpreter). From source, this file
    is <repo>/common/app_paths.py, so the repo root is two levels up.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir(name):
    """Return <base_dir>/<name>, creating the folder (and parents) if it does not exist yet."""
    path = os.path.join(base_dir(), name)
    os.makedirs(path, exist_ok=True)
    return path
