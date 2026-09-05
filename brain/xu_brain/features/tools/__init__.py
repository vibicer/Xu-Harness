"""Tools package — aggregates all toolsets."""
from __future__ import annotations

from typing import Any
from .base import Tool


def all_tools() -> list[Tool]:
    tools: list[Tool] = []
    from .agents import ask, delegate, preset_create, subagent_interrupt, subagent_list, subagent_message
    from .eval_tool import eval as eval_tool
    from .file import edit, read, write
    from .github_tool import github
    from .image import inspect_image, show_image
    from .memory_tools import memory_edit, recall, reflect, retain_tool
    from .plan import todo
    from .search import glob, grep
    from .skills_tools import manage_skill, skill_list, skill_load
    from .terminal import bash
    from .toolbuilder import tool_create, tool_list, tool_remove
    from .web import web_extract, web_search

    tools += [bash, read, write, edit, glob, grep, eval_tool, web_search, web_extract,
              github, inspect_image, show_image, todo, ask, delegate, preset_create,
              subagent_list, subagent_message, subagent_interrupt, memory_edit, retain_tool,
              recall, reflect, skill_list, skill_load, manage_skill, tool_create, tool_list,
              tool_remove]
    try:
        from .ast import ast_edit, ast_grep
        tools += [ast_grep, ast_edit]
    except Exception:
        pass
    try:
        from .browser import browse, screenshot
        tools += [browse, screenshot]
    except Exception:
        pass
    try:
        from .lsp_debug import debug, lsp
        tools += [lsp, debug]
    except Exception:
        pass
    return tools


TOOLSETS: list[dict[str, Any]] = [
    {"toolset": "terminal", "tools": ["bash"]},
    {"toolset": "file", "tools": ["read", "write", "edit"]},
    {"toolset": "search", "tools": ["glob", "grep"]},
    {"toolset": "ast", "tools": ["ast_grep", "ast_edit"]},
    {"toolset": "lsp", "tools": ["lsp"]},
    {"toolset": "debug", "tools": ["debug"]},
    {"toolset": "eval", "tools": ["eval"]},
    {"toolset": "image", "tools": ["inspect_image", "show_image"]},
    {"toolset": "web", "tools": ["web_search", "web_extract"]},
    {"toolset": "browser", "tools": ["browse", "screenshot"]},
    {"toolset": "github", "tools": ["github"]},
    {"toolset": "agents", "tools": ["ask"]},
    {"toolset": "orchestration", "tools": ["delegate", "preset_create", "subagent_list", "subagent_message", "subagent_interrupt"]},
    {"toolset": "plan", "tools": ["todo"]},
    {"toolset": "memory", "tools": ["memory_edit", "retain", "recall", "reflect"]},
    {"toolset": "skills", "tools": ["skill_list", "skill_load", "manage_skill"]},
    {"toolset": "toolbuilder", "tools": ["tool_create", "tool_list", "tool_remove"]},
]
