"""Runnable check: exercise the filebrowser plugin through the real PluginHost.

Covers the contract the shell depends on and the security posture that matters:
tree/read/write/create/remove round-trip, cwd containment (absolute path and
`..` traversal), the concurrent-write tag guard, and the non-recursive delete.

Run: `cd brain && uv run python ~/.xu/plugins/filebrowser/_check.py`
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from xu_brain.core.bus import HookBus
from xu_brain.core.contract import RpcError
from xu_brain.core.host import PluginHost


class FakeSession:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd


class FakeSessions:
    def __init__(self, cwd: str) -> None:
        self._s = FakeSession(cwd)

    def get(self, _sid):
        return self._s


class FakeConfig:
    def __init__(self) -> None:
        self._d: dict = {}

    def all(self) -> dict:
        return dict(self._d)

    def set(self, k, v) -> None:
        self._d[k] = v


class FakeApp:
    """Minimal App surface the plugin touches: sessions, config, register_handler."""

    def __init__(self, cwd: str) -> None:
        self.sessions = FakeSessions(cwd)
        self.config = FakeConfig()
        self.methods: dict = {}

    def register_handler(self, name, handler):
        self.methods[name] = handler
        return lambda: self.methods.pop(name, None)


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" != {want!r}"))
    return ok


async def expect_error(label: str, coro, needle: str) -> bool:
    try:
        await coro
    except RpcError as exc:
        ok = needle in str(exc)
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {exc}")
        return ok
    print(f"FAIL {label}: expected RpcError containing {needle!r}, got success")
    return False


async def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="fb-check-"))
    (root / "sub").mkdir()
    (root / "hello.txt").write_text("first\nsecond\n", encoding="utf-8")
    (root / "sub" / "nested.md").write_text("# nested\n", encoding="utf-8")
    outside = root.parent / f"{root.name}-outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    app = FakeApp(str(root))
    host = PluginHost(HookBus())
    # Load from a throwaway copy: enable() persists runtime state
    # (plugins.enabled.json) into the scanned root, and the source tree
    # must stay clean.
    pkg_src = Path(__file__).resolve().parent
    plugins_root = Path(tempfile.mkdtemp(prefix="fb-check-plugins-"))
    shutil.copytree(
        pkg_src, plugins_root / pkg_src.name,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    host.load_dir(plugins_root, app)
    await host.ready()
    # The check must pass regardless of the user's enabled state. enable() also
    # activates on the spot, so this doubles as a check that toggling a plugin
    # on binds its rpcs without a reload.
    if not any(p["name"] == "filebrowser" and p["enabled"] for p in host.list()):
        host.enable("filebrowser")
        await host.ready()

    m = app.methods
    results: list[bool] = []
    results.append(check("rpcs registered", sorted(k for k in m if k.startswith("fs.")),
                         ["fs.read", "fs.remove", "fs.tree", "fs.write"]))

    p = {"session_id": "s1"}

    # ---- tree ---------------------------------------------------------- #
    tree = await m["fs.tree"]({**p, "path": ""})
    results.append(check("root listing (dirs first)", [e["name"] for e in tree["entries"]],
                         ["sub", "hello.txt"]))
    results.append(check("root parent is None", tree["parent"], None))
    results.append(check("file size reported", tree["entries"][1]["size"], 13))

    sub = await m["fs.tree"]({**p, "path": "sub"})
    results.append(check("subdir parent is root", sub["parent"], ""))

    # ---- read ---------------------------------------------------------- #
    got = await m["fs.read"]({**p, "path": "hello.txt"})
    results.append(check("read content", got["content"], "first\nsecond\n"))
    tag = got["tag"]

    # ---- write with a correct tag ------------------------------------- #
    wrote = await m["fs.write"]({**p, "path": "hello.txt", "content": "changed\n", "tag": tag})
    results.append(check("write not flagged as created", wrote["created"], False))
    results.append(check("write landed on disk", (root / "hello.txt").read_text(), "changed\n"))

    # ---- the stale-tag guard (agent wrote underneath us) -------------- #
    results.append(await expect_error(
        "stale tag refused",
        m["fs.write"]({**p, "path": "hello.txt", "content": "clobber\n", "tag": tag}),
        "changed on disk",
    ))
    results.append(
        check("refused write left content alone", (root / "hello.txt").read_text(), "changed\n")
    )

    # ---- create ------------------------------------------------------- #
    created = await m["fs.write"]({**p, "path": "sub/new.txt", "content": "x\n"})
    results.append(check("create flagged", created["created"], True))
    results.append(check("created file exists", (root / "sub" / "new.txt").is_file(), True))

    # ---- containment -------------------------------------------------- #
    results.append(await expect_error(
        "absolute path outside cwd refused",
        m["fs.read"]({**p, "path": str(outside)}),
        "escapes the session cwd",
    ))
    results.append(await expect_error(
        "dotdot traversal refused",
        m["fs.read"]({**p, "path": f"../{outside.name}"}),
        "escapes the session cwd",
    ))
    results.append(await expect_error(
        "traversal via subdir refused",
        m["fs.read"]({**p, "path": f"sub/../../{outside.name}"}),
        "escapes the session cwd",
    ))
    results.append(await expect_error(
        "write outside cwd refused",
        m["fs.write"]({**p, "path": "../escaped.txt", "content": "no"}),
        "escapes the session cwd",
    ))
    results.append(check("no file escaped", (root.parent / "escaped.txt").exists(), False))

    # ---- remove ------------------------------------------------------- #
    results.append(await expect_error(
        "non-empty dir not removed",
        m["fs.remove"]({**p, "path": "sub"}),
        "cannot remove",
    ))
    await m["fs.remove"]({**p, "path": "sub/new.txt"})
    await m["fs.remove"]({**p, "path": "sub/nested.md"})
    removed = await m["fs.remove"]({**p, "path": "sub"})
    results.append(check("empty dir removed", removed["removed"], True))
    results.append(check("dir gone", (root / "sub").exists(), False))
    results.append(await expect_error(
        "refuses to remove the cwd itself",
        m["fs.remove"]({**p, "path": ""}),
        "refusing to remove",
    ))

    # ---- binary + size caps ------------------------------------------- #
    (root / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
    results.append(await expect_error(
        "binary read refused",
        m["fs.read"]({**p, "path": "blob.bin"}),
        "binary file",
    ))
    app.config.set("plugin.filebrowser.max_bytes", 2048)
    (root / "big.txt").write_text("z" * 5000, encoding="utf-8")
    results.append(await expect_error(
        "oversize read refused",
        m["fs.read"]({**p, "path": "big.txt"}),
        "limit is 2048",
    ))
    results.append(await expect_error(
        "oversize write refused",
        m["fs.write"]({**p, "path": "big2.txt", "content": "z" * 5000}),
        "limit is 2048",
    ))

    # ---- ui asset gating --------------------------------------------- #
    results.append(check("ui.js served while enabled",
                         host.ui_asset("filebrowser", "ui.js") is not None, True))
    results.append(check("undeclared file not served",
                         host.ui_asset("filebrowser", "activate.py"), None))
    results.append(check("traversal not served",
                         host.ui_asset("filebrowser", "../clock/activate.py"), None))
    host.disable("filebrowser")
    results.append(check("ui.js withheld once disabled",
                         host.ui_asset("filebrowser", "ui.js"), None))
    results.append(check("rpcs unbound once disabled",
                         [k for k in app.methods if k.startswith("fs.")], []))
    host.enable("filebrowser")
    await host.ready()
    results.append(check("enable rebinds rpcs without a reload",
                         sorted(k for k in app.methods if k.startswith("fs.")),
                         ["fs.read", "fs.remove", "fs.tree", "fs.write"]))

    # ---- disposal (reload must not leave the methods bound) ----------- #
    host.deactivate_all()
    results.append(
        check("rpcs disposed on unload", [k for k in app.methods if k.startswith("fs.")], [])
    )

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
