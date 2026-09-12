"""The arrow-key chain picker behind bare `omh model-chains`.

One row per mixture category. Up and down move the cursor; on the selected
row, left and right step the head model through the aliases chains name
today, `-` and `+` step its reasoning effort, and `d` restores the shipped
default. Enter hands every edited category back to the command, which writes
them through the same validated document `omh model-chains set` writes; q,
Esc or Ctrl-C hands back nothing, so a cancelled picker cannot have touched
the file by construction.

The chain rules — which aliases the ring holds, how an effort steps, how a
save composes the document — live in the plugin bundle's
`model_chain_picker` so the Modern-TUI widget walks the same rows; this
module only paints frames and reads keys. The same seams as the theme picker
keep it testable without a terminal: `read_key`, `write` and the frame width
arrive as arguments, and the raw-mode reader is shared with that picker.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
import sys
from typing import Any, TextIO

from ..plugin_bundle.omh.model_chain_picker import (
    PICKER_EFFORT_RING,
    Chain,
    chain_from_entries,
    chain_text,
    step_effort,
    step_head_model,
)
from .theme_picker import (
    BOLD,
    CLEAR_LINE,
    ESC,
    HIDE_CURSOR,
    KEY_DEFAULT,
    KEY_DOWN,
    KEY_ENTER,
    KEY_LEFT,
    KEY_MINUS,
    KEY_PLUS,
    KEY_QUIT,
    KEY_RIGHT,
    KEY_UP,
    RESET,
    SHOW_CURSOR,
    cursor_up,
    raw_mode,
    read_terminal_key,
)

DIM = f"{ESC}[2m"

_HEADER = "Model chains  (up/down category, left/right head model, -/+ effort, d default, Enter save, q cancel)"
# Column widths include one trailing space so a cell that fills its width
# never runs into the next one.
_CATEGORY_WIDTH = 20
_PURPOSE_WIDTH = 31
_MODEL_WIDTH = 23


def effort_bar(effort: str) -> str:
    """Four cells, one per ring rung; `max` fills all four, nothing fills none."""
    if effort == "max":
        filled = len(PICKER_EFFORT_RING)
    elif effort in PICKER_EFFORT_RING:
        filled = PICKER_EFFORT_RING.index(effort) + 1
    else:
        filled = 0
    return "■" * filled + "□" * (len(PICKER_EFFORT_RING) - filled)


def _paint(text: str, code: str, use_color: bool) -> str:
    return f"{code}{text}{RESET}" if use_color else text


def _fit(text: str, width: int) -> str:
    """Pad to `width` cells, the last of them always blank."""
    if len(text) >= width:
        return text[: width - 2] + "… "
    return text.ljust(width)


def row_status(row: Mapping[str, Any], chain: Chain) -> str:
    """`edited` beats the stored origin: the row shows what Enter would do."""
    if chain != chain_from_entries(row["chain"]):
        return "edited"
    return str(row["origin"])


def render_frame(
    payload: Mapping[str, Any],
    chains: Mapping[str, Chain],
    cursor: int,
    *,
    use_color: bool,
    width: int,
) -> list[str]:
    """One full repaint: the header, one row per category, the cursor row's
    detail, then the save line."""
    rows = payload["categories"]
    labels = {row["alias"]: row["label"] for row in payload["models"]}
    served = {row["alias"]: bool(row["served"]) for row in payload["models"]}
    lines = [_HEADER]
    for index, row in enumerate(rows):
        chain = chains[row["category"]]
        head_model, head_effort = chain[0]
        pointer = ">" if index == cursor else " "
        unserved = "!" if not served.get(head_model, True) else " "
        line = (
            f" {pointer} {_fit(row['category'], _CATEGORY_WIDTH)}"
            f"{_fit(row['purpose'], _PURPOSE_WIDTH)}"
            f"{_fit(labels.get(head_model, head_model), _MODEL_WIDTH)}"
            f"{effort_bar(head_effort)} {head_effort or '-':<7}{unserved} {row_status(row, chain)}"
        )
        lines.append(_paint(line, BOLD, use_color) if index == cursor else line)
    current = rows[cursor]
    chain = chains[current["category"]]
    lines.append("")
    lines.append(f"    [{current['category']}] {current['purpose']}".rstrip())
    lines.append(f"    chain: {chain_text(chain)}")
    default_chain = chain_from_entries(current["default_chain"])
    if chain != default_chain:
        lines.append(_paint(f"    shipped default: {chain_text(default_chain)}", DIM, use_color))
    if any(not served.get(chains[row["category"]][0][0], True) for row in rows):
        lines.append(_paint("    ! this machine's recorded providers do not serve that head", DIM, use_color))
    changed = sum(1 for row in rows if chains[row["category"]] != chain_from_entries(row["chain"]))
    if changed:
        lines.append(f"    {changed} unsaved change{'s' if changed != 1 else ''}; Enter writes {payload['path']}")
    else:
        lines.append("    no unsaved changes; Enter or q leaves the file as it is")
    # Plain rows are cut to the terminal width; a painted row carries escape
    # bytes no column count applies to, and cutting one mid-escape would leak
    # the sequence into the terminal (the theme picker's rule).
    return [line if use_color or len(line) <= width else line[: max(0, width)] for line in lines]


def run_picker(
    payload: Mapping[str, Any],
    *,
    read_key: Callable[[], str],
    write: Callable[[str], None],
    use_color: bool,
    width: int = 110,
    max_keys: int = 4000,
) -> dict[str, Chain] | None:
    """Drive the cursor until Enter or cancel.

    Returns the edited categories (possibly none) on Enter, or None on
    cancel. Writes nothing but frames: applying the result is the caller's
    job.
    """
    rows = payload["categories"]
    if not rows:
        return None
    aliases = tuple(row["alias"] for row in payload["models"])
    original = {row["category"]: chain_from_entries(row["chain"]) for row in rows}
    chains = dict(original)
    cursor = 0
    frame = render_frame(payload, chains, cursor, use_color=use_color, width=width)
    write("\n".join(frame) + "\n")
    # `max_keys` bounds a pathological reader (a stream returning nothing but
    # unknown bytes) instead of spinning forever inside a terminal in raw mode.
    for _ in range(max_keys):
        key = read_key()
        if key == KEY_QUIT:
            return None
        if key == KEY_ENTER:
            return {name: chain for name, chain in chains.items() if chain != original[name]}
        name = rows[cursor]["category"]
        if key == KEY_UP:
            cursor = (cursor - 1) % len(rows)
        elif key == KEY_DOWN:
            cursor = (cursor + 1) % len(rows)
        elif key == KEY_LEFT:
            chains[name] = step_head_model(chains[name], aliases, -1)
        elif key == KEY_RIGHT:
            chains[name] = step_head_model(chains[name], aliases, 1)
        elif key == KEY_MINUS:
            chains[name] = step_effort(chains[name], -1)
        elif key == KEY_PLUS:
            chains[name] = step_effort(chains[name], 1)
        elif key == KEY_DEFAULT:
            chains[name] = chain_from_entries(rows[cursor]["default_chain"])
        else:
            continue
        previous_rows = len(frame)
        frame = render_frame(payload, chains, cursor, use_color=use_color, width=width)
        write(cursor_up(previous_rows) + "".join(f"{CLEAR_LINE}{line}\n" for line in frame))
    return None


def pick_chains_interactively(
    payload: Mapping[str, Any],
    *,
    use_color: bool,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> dict[str, Chain] | None:
    """`run_picker` wired to the real terminal, cursor hidden for the duration."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    width = os.get_terminal_size(sink.fileno()).columns if sink.isatty() else 110
    with raw_mode(source):
        if use_color:
            sink.write(HIDE_CURSOR)
        try:
            return run_picker(
                payload,
                read_key=lambda: read_terminal_key(source),
                write=lambda text: (sink.write(text), sink.flush(), None)[-1],
                use_color=use_color,
                width=width,
            )
        finally:
            if use_color:
                sink.write(SHOW_CURSOR)
                sink.flush()
