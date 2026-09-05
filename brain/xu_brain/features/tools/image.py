"""image toolset — inspect_image via provider vision, show_image into the chat."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult


class InspectImageTool(Tool):
    name = "inspect_image"
    toolset = "image"
    description = (
        "Analyze a local image file with a vision-capable model. Returns the "
        "model's text answer. Requires a configured provider with a vision model."
    )
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "local image path"},
            "question": {"type": "string", "description": "what to inspect"},
        },
        "required": ["path", "question"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = str(args.get("path", ""))
        question = str(args.get("question", ""))
        if not path or not question:
            return ToolResult.err("path and question required")
        p = Path(path)
        if not p.is_absolute():
            p = Path(ctx.cwd) / p
        if not p.exists():
            return ToolResult.err(f"not found: {p}")
        data = p.read_bytes()
        b64 = base64.b64encode(data).decode()
        mime = _guess_mime(p)
        # Route to the provider that actually hosts the model: configured
        # vision_model → session model → first provider's first model. Picking
        # providers[0] blindly 404s whenever another provider owns the model.
        model = None
        if isinstance(ctx.config, dict):
            model = ctx.config.get("vision_model") or ctx.config.get("model")
        resolved = ctx.providers.resolve(model) if model else None
        if resolved is not None:
            provider, model = resolved
        else:
            providers = ctx.providers.list()
            if not providers:
                return ToolResult.err("no provider configured for vision")
            provider = providers[0]
            if model is None and provider.models:
                model = provider.models[0]
            if model is None:
                return ToolResult.err("no vision model available")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ],
            }
        ]
        out: list[str] = []
        async for ev in ctx.providers.chat_stream(provider, model, messages):  # type: ignore[attr-defined]
            if ev.delta:
                out.append(ev.delta)
            if ev.error:
                return ToolResult.err(ev.error)
        text = "".join(out) or "(no response)"
        return ToolResult.ok(text, raw=text)


class ShowImageTool(Tool):
    name = "show_image"
    toolset = "image"
    description = (
        "Show a local image file to the user in the chat transcript (png, jpeg, gif, "
        "webp, svg, bmp, avif). Use it whenever the user should SEE an image you "
        "produced or found — a screenshot, a rendered chart, a diagram, a generated "
        "asset. This only displays; use inspect_image to read one."
    )
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "image file path (relative to cwd, or absolute)"},
            "caption": {"type": "string",
                        "description": "short caption, also used as the alt text"},
        },
        "required": ["path"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = str(args.get("path", "")).strip()
        if not path:
            return ToolResult.err("path required")
        p = Path(path)
        if not p.is_absolute():
            p = Path(ctx.cwd) / p
        if not p.is_file():
            return ToolResult.err(f"not found: {p}")
        mime = _display_mime(p)
        if not mime:
            return ToolResult.err(f"not an image (unsupported type): {p.name}")
        size = p.stat().st_size
        # Checked before reading: the data URL travels over the WebSocket and is
        # stored in the display transcript, so an unbounded file would bloat both.
        if size > MAX_IMAGE_BYTES:
            return ToolResult.err(
                f"too large to display: {p.name} is {size // 1024} KB, "
                f"cap is {MAX_IMAGE_BYTES // 1024} KB — resize or crop it first"
            )
        try:
            data = p.read_bytes()
        except OSError as e:
            return ToolResult.err(f"read failed: {e}")
        b64 = base64.b64encode(data).decode()
        caption = str(args.get("caption", "")).strip() or p.name
        # The data URL rides on the chip meta, never in `output`: the model pays
        # no tokens for pixels it already knows about.
        return ToolResult.ok(
            f"shown to the user: {p.name} ({size // 1024 or 1} KB, {mime})",
            raw=str(p),
            image=f"data:{mime};base64,{b64}",
            image_alt=caption,
        )


# Cap mirrors the composer's own per-image attachment limit (web/src/lib/attach.ts).
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Displayable-only types, kept out of `_guess_mime` because the vision APIs
# behind inspect_image reject them.
_DISPLAY_ONLY_MIME = {
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
}


def _display_mime(p: Path) -> str:
    """Mime for an image the browser can render, or "" when it is not an image."""
    extra = _DISPLAY_ONLY_MIME.get(p.suffix.lower())
    if extra:
        return extra
    mime = _guess_mime(p)
    return mime if mime.startswith("image/") else ""


def _guess_mime(p: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(p.suffix.lower(), "application/octet-stream")


inspect_image = InspectImageTool()
show_image = ShowImageTool()
