#!/usr/bin/env bash
# OWNER: P1
# build.sh: build the Linux executables WhiteboardServer, WhiteboardClient and
# WhiteboardClient-console into dist/linux/.
#
#     bash scripts/build.sh            (uses python3; or: PYTHON=python3.11 bash scripts/build.sh)
#
# Steps:
#   1. create a FRESH virtual environment .build_venv (deleted first) from a
#      plain python3. Never from Anaconda: PyInstaller would bundle hundreds of
#      MB of unrelated conda libraries into the executables.
#   2. pip install -r requirements.txt pyinstaller (PyQt5 from pip brings its own
#      Qt, including QtMultimedia for the join/leave sounds).
#   3. pyinstaller --clean on each spec in packaging/.
#   4. copy README_RUN.txt and a firewall hint next to the executables, print sizes.
# Executables are NOT committed (dist/ and build/ are in .gitignore); they go in the zip.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
VENV="$ROOT/.build_venv"
DIST="$ROOT/dist/linux"
WORK="$ROOT/build/linux"
PYINSTALLER_VERSION="6.11.1"

# A PYTHONPATH from the shell (e.g. ROS: /opt/ros/humble/...) comes BEFORE the
# venv in sys.path, so pip and PyInstaller could pick up and bundle foreign
# packages. The build must see only the venv.
unset PYTHONPATH PYTHONHOME

# --- 1. refuse Anaconda / Miniconda ------------------------------------------
if [ -n "${CONDA_PREFIX:-}" ] || "$PYTHON" -c "import os,sys; sys.exit(0 if os.path.isdir(os.path.join(sys.base_prefix,'conda-meta')) else 1)"; then
    echo "ERROR: $PYTHON is an Anaconda/conda Python. Run 'conda deactivate' and use the system python3" >&2
    echo "       (or PYTHON=/usr/bin/python3 bash scripts/build.sh)." >&2
    exit 1
fi
"$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" \
    || { echo "ERROR: Python 3.10 or newer is needed." >&2; exit 1; }

echo "== Fresh virtual environment: $VENV"
rm -rf "$VENV"
"$PYTHON" -m venv "$VENV"
VPY="$VENV/bin/python"

# --- 2. dependencies -----------------------------------------------------------
echo "== Installing requirements and PyInstaller $PYINSTALLER_VERSION"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -r requirements.txt "pyinstaller==$PYINSTALLER_VERSION"
"$VPY" -m pip list 2>/dev/null | grep -iE "^(pyqt5|pyinstaller)" || true

# --- 3. build ------------------------------------------------------------------
rm -rf "$DIST"
mkdir -p "$DIST" "$WORK"
LOG="$WORK/pyinstaller.log"
: > "$LOG"
for spec in WhiteboardServer WhiteboardClient WhiteboardClient-console; do
    echo "== PyInstaller: $spec"
    "$VPY" -m PyInstaller --clean --noconfirm --log-level WARN \
        --distpath "$DIST" --workpath "$WORK" "packaging/$spec.spec" 2>&1 | tee -a "$LOG"
done

# --- 4. extras next to the executables -----------------------------------------
cp packaging/README_RUN.txt "$DIST/"
cat > "$DIST/FIREWALL_LINUX.txt" <<'EOF'
Linux firewall (server laptop only, needed once).
If other laptops time out, allow the whiteboard port:
    sudo ufw allow 5050/tcp        (Ubuntu with ufw enabled; check: sudo ufw status)
    sudo firewall-cmd --add-port=5050/tcp     (Fedora / firewalld)
EOF
chmod +x "$DIST/WhiteboardServer" "$DIST/WhiteboardClient" "$DIST/WhiteboardClient-console"

echo
echo "== Missing-module warnings (full log: $LOG; details: $WORK/*/warn-*.txt)"
grep -E "WARNING" "$LOG" || echo "   (none)"
echo
echo "== Result in $DIST"
ls -lh "$DIST"
du -sh "$DIST"
echo "Smoke test:  $VPY scripts/exe_smoke_test.py --dist $DIST"
