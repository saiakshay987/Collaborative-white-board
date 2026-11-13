"""make_submission.py: Build the lab submission zip (executables, source, report, slides, video).

Usage (from anywhere; standard library only, git must be installed):

    python scripts/make_submission.py --windows-dir <folder with the Windows exe files> \
        --linux-dir <folder with the Linux files> --pdf <technical pdf> --ppt <pptx> \
        --video <video file> [--out LAN_Whiteboard_Submission.zip]

The zip has one top folder, LAN_Whiteboard_Submission/, with:

    README.md
    executables/windows/   WhiteboardServer.exe, WhiteboardClient.exe, README_RUN.txt
    executables/linux/     WhiteboardServer, WhiteboardClient, README_RUN.txt
    source/                every file from `git ls-files`, minus the internal working notes
    documentation/         the PDF
    presentation/          the PPT
    video/                 the video

The "-console" debugging client is never packed. If the executable folders also hold the
firewall helpers that scripts/build.bat and scripts/build.sh create (open_firewall.ps1,
FIREWALL_LINUX.txt), they are packed too, because README_RUN.txt refers to them.

Everything is checked BEFORE the zip is written. The script stops with a clear message
(exit status 1, nothing written) if

  * a required argument path is missing or has the wrong kind (folder or file),
  * an expected executable or README_RUN.txt is missing from the Windows or Linux folder,
  * a file in source/ has a name that starts like p1_ to p4_, or contains a comment line that
    names an owner (P1 to P4): these were removed from the project and must not come back,
  * server_data/ (or anything else that must never be shipped) would end up in the zip.

The source comes from `git ls-files`, so untracked files are not included, and files that are
tracked but changed on disk are packed as they are on disk (a warning says so). Commit first.
When the zip is written, its tree and its total size in MB are printed.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import zipfile

# Folder of this file (scripts/) and the repository root above it.
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = os.path.dirname(SCRIPTS_DIR)

# The single top-level folder inside the zip.
TOP = "LAN_Whiteboard_Submission"

# Files each executable folder must contain. The console variant is NOT listed and never packed.
WINDOWS_FILES = ["WhiteboardServer.exe", "WhiteboardClient.exe", "README_RUN.txt"]
LINUX_FILES = ["WhiteboardServer", "WhiteboardClient", "README_RUN.txt"]
# Packed only when present: the firewall helpers that the build scripts put next to the executables.
WINDOWS_OPTIONAL = ["open_firewall.ps1"]
LINUX_OPTIONAL = ["FIREWALL_LINUX.txt"]
# Linux binaries get the executable bit inside the zip (a zip made on Windows has none).
LINUX_EXECUTABLE = {"WhiteboardServer", "WhiteboardClient"}

# Internal working notes that stay in the repository but not in the submission.
EXCLUDED_FILES = {"CLAUDE.md", "docs/CONTRACT.md", "docs/ui_handoff_for_akshay.md"}
EXCLUDED_PREFIXES = ("docs/sections/", "docs/final/")
# Folders (as any path component) and file types that never belong in the zip.
NEVER_COMPONENTS = {"server_data", "__pycache__", "dist", "build", ".git"}
NEVER_SUFFIXES = (".pyc", ".pyo")

# A file name that starts like p1_ .. p4_ (at the start of the name or after a separator).
OWNER_NAME = re.compile(r"(?i)(^|[/\\._-])p[1-4]_")
# A comment line that names an owner, for example "<#> OWNER: P2" or an HTML comment with it.
OWNER_LINE = re.compile(r"(?im)^[ \t]*(?:#|//|<!--|/\*)?[ \t]*owner[ \t]*:[ \t]*p[1-4]\b")

# Do not scan files bigger than this for owner lines (they are not text we wrote).
MAX_SCAN_BYTES = 2 * 1024 * 1024

# Already compressed formats: store them instead of wasting time deflating them again.
STORED_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".zip", ".pptx", ".docx", ".png", ".jpg"}


class SubmissionError(Exception):
    """A check failed; the message tells the user what to fix. Nothing has been written."""


def fail(message):
    """Raise SubmissionError with message (one place, so every failure reads the same)."""
    raise SubmissionError(message)


# --- reading the repository ------------------------------------------------------------

def git(repo, *args):
    """Run `git <args>` in repo and return stdout as bytes; fail clearly if git or the repo is missing."""
    try:
        done = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        fail("cannot run git (%s). Install git and run this from a clone of the repository." % exc)
    if done.returncode != 0:
        fail("git %s failed in %s: %s" % (" ".join(args), repo, done.stderr.decode("utf-8", "replace").strip()))
    return done.stdout


def tracked_files(repo):
    """Return [(path, mode)] of every file git tracks: path with forward slashes, mode like '100644'.

    `git ls-files -s -z` prints "<mode> <sha> <stage>\\t<path>" separated by NUL bytes, so
    names with spaces or non-ASCII characters are exact.
    """
    out = git(repo, "ls-files", "-s", "-z").decode("utf-8")
    files = []
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        files.append((path, meta.split()[0]))
    return files


def warn_if_dirty(repo):
    """Print a warning when tracked files differ from the last commit (they are packed as on disk)."""
    out = git(repo, "status", "--porcelain", "--untracked-files=no").decode("utf-8", "replace")
    if out.strip():
        print("WARNING: tracked files have uncommitted changes; they are packed as they are on disk:")
        for line in out.splitlines()[:10]:
            print("   " + line)


def is_excluded(path):
    """True for a tracked file that must not go into source/ (internal notes, junk folders)."""
    if path in EXCLUDED_FILES or path.startswith(EXCLUDED_PREFIXES):
        return True
    parts = path.split("/")
    if any(part in NEVER_COMPONENTS for part in parts):
        return True
    return path.endswith(NEVER_SUFFIXES)


# --- checks ------------------------------------------------------------------------------

def check_inputs(args):
    """Check every required path argument; fail with one message listing everything that is wrong."""
    problems = []

    def need_dir(label, path, names):
        """The folder must exist and contain every file of names."""
        if not os.path.isdir(path):
            problems.append("%s: folder not found: %s" % (label, path))
            return
        for name in names:
            if not os.path.isfile(os.path.join(path, name)):
                problems.append("%s: %s is missing from %s" % (label, name, path))

    def need_file(label, path, suffixes=None):
        """The file must exist; a surprising extension is only a warning."""
        if not os.path.isfile(path):
            problems.append("%s: file not found: %s" % (label, path))
        elif suffixes and not path.lower().endswith(suffixes):
            print("WARNING: %s %s does not end in %s" % (label, path, " or ".join(suffixes)))

    need_dir("--windows-dir", args.windows_dir, WINDOWS_FILES)
    need_dir("--linux-dir", args.linux_dir, LINUX_FILES)
    need_file("--pdf", args.pdf, (".pdf",))
    need_file("--ppt", args.ppt, (".pptx", ".ppt"))
    need_file("--video", args.video)
    if not os.path.isfile(os.path.join(args.repo, "README.md")):
        problems.append("README.md not found in %s" % args.repo)
    if problems:
        fail("missing or wrong input:\n  " + "\n  ".join(problems))


def looks_like_text(data):
    """True if data is probably text (no NUL byte); binary files are not scanned for owner lines."""
    return b"\0" not in data[:4096]


def check_source(repo, source):
    """Check the files chosen for source/: no owner leftovers, nothing that must never ship.

    source is a list of (path, mode). Fails listing every offender.
    """
    problems = []
    for path, _mode in source:
        if OWNER_NAME.search(path):
            problems.append("file name looks like an owner file (p1_ to p4_): " + path)
        if any(part == "server_data" for part in path.split("/")):
            problems.append("server_data/ would be included: " + path)
        full = os.path.join(repo, path)
        if not os.path.isfile(full) or os.path.getsize(full) > MAX_SCAN_BYTES:
            continue
        with open(full, "rb") as f:
            data = f.read()
        if not looks_like_text(data):
            continue
        text = data.decode("utf-8", "replace")
        match = OWNER_LINE.search(text)
        if match:
            line_no = text.count("\n", 0, match.start()) + 1
            problems.append("owner line in %s, line %d: %s" % (path, line_no, match.group(0).strip()))
    if problems:
        fail("source/ is not clean:\n  " + "\n  ".join(problems))


# --- building the list of zip entries ----------------------------------------------------

def plan_entries(args):
    """Return the list of (archive name, file on disk, unix mode, compress type) for the whole zip.

    Also runs the checks, so nothing is written unless the plan is valid.
    """
    check_inputs(args)
    warn_if_dirty(args.repo)

    entries = []

    def add(arcname, path, mode=0o644):
        """Append one entry; stored (not deflated) when the file type is already compressed."""
        compress = zipfile.ZIP_STORED if os.path.splitext(path)[1].lower() in STORED_SUFFIXES \
            else zipfile.ZIP_DEFLATED
        entries.append((TOP + "/" + arcname, path, mode, compress))

    # README at the top: the working-tree file (it is also inside source/).
    add("README.md", os.path.join(args.repo, "README.md"))

    for name in WINDOWS_FILES + [n for n in WINDOWS_OPTIONAL if os.path.isfile(os.path.join(args.windows_dir, n))]:
        add("executables/windows/" + name, os.path.join(args.windows_dir, name))
    for name in LINUX_FILES + [n for n in LINUX_OPTIONAL if os.path.isfile(os.path.join(args.linux_dir, n))]:
        add("executables/linux/" + name, os.path.join(args.linux_dir, name),
            0o755 if name in LINUX_EXECUTABLE else 0o644)

    tracked = tracked_files(args.repo)
    if not any(path == "README.md" for path, _ in tracked):
        fail("README.md is not tracked by git; run `git add README.md` and commit it first.")
    source = [(p, m) for p, m in tracked if not is_excluded(p)]
    shipped_data = [p for p, _ in tracked if "server_data" in p.split("/")]
    if shipped_data:
        print("NOTE: %d tracked file(s) under server_data/ are left out of source/ (never shipped)." % len(shipped_data))
    # A tracked file that was deleted from disk cannot be packed; say so instead of crashing later.
    gone = [p for p, _ in source if not os.path.isfile(os.path.join(args.repo, p))]
    if gone:
        fail("tracked files are missing on disk (restore or `git rm` them):\n  " + "\n  ".join(gone))
    check_source(args.repo, source)
    for path, mode in source:
        add("source/" + path, os.path.join(args.repo, path), 0o755 if mode == "100755" else 0o644)

    add("documentation/" + os.path.basename(args.pdf), args.pdf)
    add("presentation/" + os.path.basename(args.ppt), args.ppt)
    add("video/" + os.path.basename(args.video), args.video)

    final_check(entries)
    return entries


def final_check(entries):
    """Last safety net over the finished plan: the things that must never be in the zip."""
    bad = []
    names = set()
    for arcname, _path, _mode, _compress in entries:
        parts = arcname.split("/")
        if "server_data" in parts:
            bad.append("server_data/ would be included: " + arcname)
        if "executables" in parts and "-console" in parts[-1]:
            bad.append("the console executable must not be packed: " + arcname)
        if arcname in names:
            bad.append("two entries with the same name: " + arcname)
        names.add(arcname)
    if bad:
        fail("zip plan is not valid:\n  " + "\n  ".join(bad))


# --- writing and showing the zip ---------------------------------------------------------

def write_zip(entries, out_path):
    """Write the zip to a temporary file next to out_path, then move it into place.

    A failure half-way therefore never leaves a broken zip under the final name.
    """
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".submission_", suffix=".tmp", dir=out_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w") as zf:
            for arcname, path, mode, compress in entries:
                info = zipfile.ZipInfo.from_file(path, arcname)
                info.compress_type = compress
                # Unix permission bits live in the high 16 bits of external_attr.
                info.external_attr = (0o100000 | mode) << 16
                with open(path, "rb") as src, zf.open(info, "w", force_zip64=os.path.getsize(path) > 0x7FFFFFFF) as dst:
                    while True:
                        block = src.read(1024 * 1024)
                        if not block:
                            break
                        dst.write(block)
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def print_tree(out_path):
    """Print every folder and file of the written zip as an indented tree, then the total size."""
    with zipfile.ZipFile(out_path) as zf:
        infos = sorted(zf.infolist(), key=lambda i: i.filename)
    seen = set()
    print()
    for info in infos:
        parts = info.filename.split("/")
        for depth in range(len(parts) - 1):
            folder = "/".join(parts[:depth + 1])
            if folder not in seen:
                seen.add(folder)
                print("%s%s/" % ("    " * depth, parts[depth]))
        size_kb = info.file_size / 1024.0
        print("%s%s  (%.1f KB)" % ("    " * (len(parts) - 1), parts[-1], size_kb))
    total_mb = os.path.getsize(out_path) / (1024.0 * 1024.0)
    print("\n%d files, zip size %.2f MB: %s" % (len(infos), total_mb, os.path.abspath(out_path)))


def parse_args(argv):
    """Read the command line (see the module docstring)."""
    parser = argparse.ArgumentParser(description="Build the LAN Whiteboard submission zip.")
    parser.add_argument("--windows-dir", required=True, help="folder with the Windows executables")
    parser.add_argument("--linux-dir", required=True, help="folder with the Linux executables")
    parser.add_argument("--pdf", required=True, help="technical report (PDF)")
    parser.add_argument("--ppt", required=True, help="presentation (pptx)")
    parser.add_argument("--video", required=True, help="demo video file")
    parser.add_argument("--out", default="LAN_Whiteboard_Submission.zip", help="zip file to write")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="repository to package (default: this one)")
    return parser.parse_args(argv)


def main(argv=None):
    """Plan, check and write the zip; return the exit status (0 ok, 1 a check failed)."""
    args = parse_args(argv)
    try:
        entries = plan_entries(args)
        write_zip(entries, args.out)
    except SubmissionError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    print_tree(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
