"""User-attached images: stored once on disk, referenced by path in the context.

The composer sends an image as a `data:` URL inside an OpenAI content-parts list.
Persisting that shape into the message history made every later turn replay the
whole base64 — costly on a vision model and fatal on a text-only one, which
answers the same 400 forever with no way to clear it.

So on the way to the provider each image part is *dereferenced*: the bytes are
written once to ``<data_home>/attachments/<session>/<sha1>.<ext>`` (content
addressed, so re-attaching the same picture reuses the file) and the row becomes
plain text carrying that path. The agent looks at it on demand with
``inspect_image``, which runs its own isolated vision call.

The display transcript is untouched — it keeps the data URL, so the shell still
renders the thumbnail.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path
from typing import Any

_DATA_URL = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.*)$", re.IGNORECASE | re.DOTALL)

_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
}

_HINT = "call inspect_image on this path to see it"


def attachments_dir(root: Path, session_id: str) -> Path:
    return root / "attachments" / session_id


def store_image(root: Path, session_id: str, url: str) -> str:
    """Persist one attached image; return the reference the model should see.

    A ``data:`` URL becomes an absolute file path. A remote URL is already a
    reference and is returned unchanged. An unreadable value returns "".
    """
    url = url.strip()
    if url.startswith(("http://", "https://")):
        return url
    match = _DATA_URL.match(url)
    if not match:
        return ""
    try:
        data = base64.b64decode(match.group(2), validate=False)
    except (binascii.Error, ValueError):
        return ""
    if not data:
        return ""
    ext = _EXT.get(match.group(1).lower(), ".img")
    dest = attachments_dir(root, session_id) / (hashlib.sha1(data).hexdigest()[:16] + ext)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return str(dest)


def dereference(content: Any, root: Path | None, session_id: str) -> Any:
    """Content-parts list → plain text with `[image attached: …]` lines.

    Strings and image-free lists pass through unchanged. ``root`` of ``None``
    (no data home — mocks and bare agents) also passes through: losing the
    attachment would be worse than replaying it.
    """
    if root is None or not isinstance(content, list):
        return content
    texts: list[str] = []
    refs: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
            continue
        holder = part.get("image_url") or part.get("image")
        url = holder.get("url") if isinstance(holder, dict) else None
        if isinstance(url, str) and url:
            refs.append(url)
    if not refs:
        return content
    lines = []
    for url in refs:
        ref = store_image(root, session_id, url)
        lines.append(f"[image attached: {ref} — {_HINT}]" if ref
                     else "[image attached: unreadable, dropped from context]")
    body = "\n\n".join(t for t in texts if t.strip())
    return f"{body}\n\n" + "\n".join(lines) if body else "\n".join(lines)


def inject(messages: list[dict[str, Any]], images: list[str] | None) -> bool:
    """Put the real pixels back into *this turn's* payload. Returns True if it did.

    Persisted history only ever holds the path (see `dereference`), so a model
    that can actually see gets the image exactly once — on the turn it was
    attached — and no later turn replays it. The reference line stays in the text
    part, so the model also learns the path and can re-inspect it later.
    """
    if not images:
        return False
    parts = _image_parts(images)
    if not parts:
        return False
    for row in reversed(messages):
        if row.get("role") != "user":
            continue
        content = row.get("content")
        text = content if isinstance(content, str) else ""
        if isinstance(content, list):
            return False  # already parts — someone injected before us
        row["content"] = ([{"type": "text", "text": text}] if text else []) + parts
        return True
    return False


def strip(messages: list[dict[str, Any]]) -> bool:
    """Take the pixels back out, keeping the text. Returns True if it changed.

    The escape hatch for a model that turns out to be blind: the request is
    rebuilt without image parts and retried, so the turn survives and the
    `[image attached: <path>]` line still tells the model what it missed.
    """
    changed = False
    for row in messages:
        content = row.get("content")
        if not isinstance(content, list):
            continue
        texts = [p["text"] for p in content
                 if isinstance(p, dict) and isinstance(p.get("text"), str)]
        had_image = any(isinstance(p, dict) and (p.get("image_url") or p.get("image"))
                        for p in content)
        if not had_image:
            continue
        row["content"] = "\n\n".join(t for t in texts if t.strip())
        changed = True
    return changed


def _image_parts(images: list[str]) -> list[dict[str, Any]]:
    """Raw image references (data: or http(s): URLs) → OpenAI content parts."""
    return [{"type": "image_url", "image_url": {"url": ref}}
            for ref in images if isinstance(ref, str) and ref]
