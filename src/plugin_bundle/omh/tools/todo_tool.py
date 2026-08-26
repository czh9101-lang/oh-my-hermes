from __future__ import annotations

import json
from typing import Any

from ..host_observation import OBSERVATION_SCHEMA, attach_public_observation, observe_plugin_tool_call
from ..runtime_reader import default_omh_home, read_omh_todo
from ..todo_store import (
    TODO_CLAIM_BOUNDARY,
    TodoStoreError,
    TodoValidationError,
    build_todo_record,
    clear_todo,
    write_todo,
)

OMH_TODO_SCHEMA = {
    "name": "omh_todo",
    "description": (
        "Declare, clear, or read the metadata-only plan todo list that OMH HUD surfaces render "
        "above the Hermes prompt input. Initialize it BEFORE starting engine work (todo init): "
        "declare numbered phases in delivery order (e.g. 'I. Bootstrap' through 'VI. Evidence "
        "and Cleanup') that cover the whole lifecycle — setup, one implement/verify/deliver "
        "task per work unit, independent review lanes, and an evidence-and-cleanup close — "
        "with one task per observable outcome, so the run walks a bounded checklist instead "
        "of an open-ended reasoning loop. Keep exactly one item active and update states as "
        "work completes. Todo items are plan declarations, never execution evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "clear", "show"],
                "description": "set writes a new todo list, clear removes it, show reads the current projection.",
            },
            "title": {
                "type": "string",
                "description": "Optional short plan title shown in the todo panel header.",
            },
            "items": {
                "type": "array",
                "description": "Todo items for action=set, in display order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Item text."},
                        "state": {
                            "type": "string",
                            "enum": ["pending", "active", "done"],
                            "description": "Item state. Defaults to pending.",
                        },
                        "phase": {
                            "type": "string",
                            "description": (
                                "Optional phase label, numbered in delivery order (e.g. "
                                "'I. Bootstrap', 'II. Wave One Delivery'). Items sharing a "
                                "phase render as one section; the HUD shows the current "
                                "phase's checklist."
                            ),
                        },
                        "depth": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 3,
                            "description": (
                                "Optional subtask nesting level (0 = top-level task, 1-3 = "
                                "subtasks rendered indented beneath the preceding shallower "
                                "item). Subtasks may omit phase; they continue their parent's "
                                "section."
                            ),
                        },
                    },
                    "required": ["text"],
                },
            },
            "omh_home": {
                "type": "string",
                "description": (
                    "Optional OMH_HOME override for action=show only. "
                    "set and clear always use the configured OMH home."
                ),
            },
            "observation": OBSERVATION_SCHEMA,
        },
        "required": ["action"],
    },
}


def omh_todo_handler(args: dict[str, Any], **kwargs) -> str:
    observation = observe_plugin_tool_call("omh_todo", args, kwargs)
    home_arg = str(args.get("omh_home", "") or "")
    action = str(args.get("action", ""))
    payload: dict[str, Any] = {
        "schema_version": "omh_todo_result/v1",
        "action": action,
        "claim_boundary": TODO_CLAIM_BOUNDARY,
    }
    # Mutations bind to the environment-configured home only: a caller-chosen
    # path would turn this metadata tool into an arbitrary-location
    # file-create/delete primitive.
    if action in {"set", "clear"} and home_arg:
        payload["status"] = "invalid_todo"
        payload["error"] = "omh_home override is read-only; set and clear use the configured OMH home"
        payload["todo"] = read_omh_todo()
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    if action == "set":
        try:
            record = build_todo_record(args.get("title", ""), args.get("items"), source="omh_todo")
            write_todo(default_omh_home(), record)
            payload["status"] = "written"
        except (TodoValidationError, TodoStoreError) as error:
            payload["status"] = "invalid_todo"
            payload["error"] = str(error)
    elif action == "clear":
        try:
            payload["status"] = "cleared" if clear_todo(default_omh_home()) else "already_absent"
        except TodoStoreError as error:
            payload["status"] = "invalid_todo"
            payload["error"] = str(error)
    elif action == "show":
        payload["status"] = "read"
    else:
        payload["status"] = "invalid_action"
        payload["error"] = "action must be set, clear, or show"
    payload["todo"] = read_omh_todo(home_arg or None)
    return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
