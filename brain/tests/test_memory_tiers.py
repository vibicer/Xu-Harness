"""MemoryStore entry_cap tier tests."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xu_brain.features.memory import MemoryStore


def test_no_entry_cap_on_multi_entry():
    """reflect() without entry_cap preserves full content (old behavior)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        data_home = Path(tmp_dir)
        store = MemoryStore(data_home)

        store.append("Short content here.")
        long_text = " ".join([f"word{i}" for i in range(100)])
        store.append(long_text)

        output = store.reflect()
        lines = output.strip().split("\n")

        assert len(lines) == 2
        assert long_text in lines[1]


def test_entry_cap_120_truncates_long_entry():
    """entry_cap=120 truncates to ~120 chars + ellipsis, word boundary honored."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        data_home = Path(tmp_dir)
        store = MemoryStore(data_home)

        short_text = "short"
        store.append(short_text)

        words = [f"word{i}" for i in range(30)]
        long_text = " ".join(words)
        store.append(long_text)

        output = store.reflect(entry_cap=120)
        lines = output.strip().split("\n")

        assert len(lines) == 2
        assert short_text in lines[0]

        # Check truncation happened
        assert " …" in lines[1]

        truncated_line = lines[1]
        body_start = truncated_line.index("] ") + 2
        body_len = len(truncated_line[body_start:-4])
        assert body_len <= 120


def test_entry_cap_zero_and_none_no_truncation():
    """entry_cap=0 and None produce no truncation."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        data_home = Path(tmp_dir)
        store = MemoryStore(data_home)

        long_text = " ".join([f"word{i}" for i in range(100)])
        store.append(long_text)

        output_none = store.reflect(entry_cap=None)
        assert long_text in output_none

        output_zero = store.reflect(entry_cap=0)
        assert long_text in output_zero


def test_only_filter_with_entry_cap():
    """only= filtering works correctly when combined with entry_cap."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        data_home = Path(tmp_dir)
        store = MemoryStore(data_home)

        store.append("Alpha entry content.", badge="user-edited")
        long_beta = "Beta entry " + " ".join([f"x{i}" for i in range(20)])
        store.append(long_beta, badge="user-edited")
        store.append("Gamma entry.", badge="user-edited")

        output = store.reflect(entry_cap=60)
        lines = output.strip().split("\n")

        assert len(lines) == 3

        for line in lines:
            body_start = line.index("] ") + 2
            body = line[body_start:].replace(" …", "")
            assert len(body) <= 60
