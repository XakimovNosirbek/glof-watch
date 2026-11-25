"""Guard: the public repo must not carry personal details, internal hosts or local paths.

The word list is base64 so the guard itself does not publish it. Add words by re-encoding the list.
Run: python -m pytest tests  (or: python tests/test_privacy.py)
"""
import base64
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORDS = base64.b64decode(
    "cm9iZXJ0YQplZGNvbQp2c3BoZXJlCmtlcmlvCmlzbW9pbG92ZGV2YXJjaGxpbnV4Cm1hY2Jvb2sKdWNlbGwKaW90YWJlawowMDA5LTAwMDgtMjc4NQ=="
).decode().split("\n")
PATTERNS = [
    re.compile(r"\b(10\.\d{1,3}|172\.(1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"),  # private IPv4
    re.compile(r"/Use[r]s/[A-Za-z]"),                                                  # local macOS paths (bracket keeps this line out of grep checks)
    re.compile(r"/home/[a-z]"),                                                        # local Linux paths
    re.compile(r"/private/tmp/claude"),                                                # local scratch paths
    re.compile(r"[A-Za-z0-9._%+-]+@(gm[a]il|yahoo|mail|outlook|icloud)\.[a-z]+", re.I),   # personal mailboxes
]
SKIP_SUFFIX = {".png", ".jpg", ".gif", ".mp4", ".webp", ".tif", ".lock"}


def tracked_text_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    for name in out.splitlines():
        p = ROOT / name
        if p.suffix.lower() in SKIP_SUFFIX or not p.is_file() or p == Path(__file__):
            continue
        try:
            yield name, p.read_text()
        except UnicodeDecodeError:
            continue


def test_no_private_details_in_tracked_files():
    files = list(tracked_text_files())
    assert files, "no tracked text files found (run inside the git checkout)"
    hits = []
    for name, text in files:
        low = text.lower()
        hits += [f"{name}: word #{i}" for i, w in enumerate(WORDS) if w and w in low]
        hits += [f"{name}: {m.group(0)}" for pat in PATTERNS for m in pat.finditer(text)]
    assert not hits, "private details in tracked files:\n" + "\n".join(hits[:50])


if __name__ == "__main__":
    test_no_private_details_in_tracked_files()
    print("ok")
