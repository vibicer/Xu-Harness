"""Example Xu plugin template — copyable reference.

The plugin loader ignores files whose stem starts with ``_`` (see
:mod:`xu_brain.plugins`), so this module is **not** a live plugin. It exists
purely as a copyable template: copy it into ``data_home/plugins/`` under a new
name (without the leading underscore) and edit the hooks you need.

Every hook is optional — delete the ones you don't use. Hooks may be sync or
async; the bus awaits coroutines and runs sync callables directly. A hook that
raises is logged and skipped (fail-soft); the value passes through unchanged.
"""


# --- hooks (all optional; remove the ones you don't need) ---

def on_start(ctx):
    """Called once when the brain starts. ``ctx`` is a :class:`ToolContext`.

    Fire-and-forget-safe: useful for warm-up, logging, or registering side
    state. Return a coroutine to run async work.
    """
    return None


def before_llm(messages):
    """Transform the OpenAI-style message list before it reaches the model.

    Return the (possibly new) list. The chain runs plugins in load order.
    """
    return messages


def after_tool(tool_name, result):
    """Transform a :class:`ToolResult` after a tool runs.

    ``tool_name`` is the registered tool name; ``result`` is the
    :class:`ToolResult`. Return the (possibly new) result.
    """
    return result


def on_message_out(content):
    """Transform the final assistant message shown to the user.

    Return the (possibly new) content string.
    """
    return content
