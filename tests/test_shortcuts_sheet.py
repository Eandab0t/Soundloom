"""The keyboard shortcuts sheet must not drift from what the player does.

A help sheet that lies is worse than no help sheet: someone presses the key,
nothing happens, and the whole sheet stops being believable. The realistic
failure is mundane - a binding is added to the player's switch and the sheet is
never updated, or a binding is removed and the sheet keeps advertising it.

There is no JS test runner in this repo, so this reads the two files as text and
compares them. That is blunt, but it catches the drift that actually happens and
it runs with the rest of the suite. The behaviour behind it - that every key
does what the sheet says, and that the sheet suspends the player while it is
open - is exercised for real in the browser.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLAYER = ROOT / "frontend" / "js" / "player.js"
SHEET = ROOT / "frontend" / "js" / "shortcuts.js"
HTML = ROOT / "frontend" / "index.html"
CSS = ROOT / "frontend" / "css" / "styles.css"

# How each key is *displayed* in the sheet maps to the `e.key.toLowerCase()`
# value the player's switch matches on. Kept here explicitly rather than derived,
# so the table itself is readable and a reviewer can check it by eye.
DISPLAY_TO_HANDLER = {
    "Space": " ",
    "←": "arrowleft",
    "→": "arrowright",
    "↑": "arrowup",
    "↓": "arrowdown",
    "M": "m",
    "N": "n",
    "P": "p",
    "S": "s",
    "R": "r",
    "Q": "q",
}

# The same table read the other way. Both sides of the comparison below are
# expressed as the chip the reader actually sees, so a row can be checked
# against the handler without translating it.
HANDLER_TO_DISPLAY = {v: k for k, v in DISPLAY_TO_HANDLER.items()}


def _handler_keys() -> set:
    """The keys the player's keydown switch actually handles."""
    text = PLAYER.read_text(encoding="utf-8")
    body = text.split("setupKeys() {", 1)[1]
    # Stop before the next top-level method so a `case` elsewhere cannot count.
    body = re.split(r"\n  [a-zA-Z_]+\(", body, maxsplit=1)[0]
    return set(re.findall(r"case '([^']+)':", body))


def _rows() -> list:
    """Every row of SHORTCUT_GROUPS as (chips, rest-of-the-object).

    `rest` is everything after the chips array, which is where the
    `gesture: true` / `system: true` flags live.
    """
    text = SHEET.read_text(encoding="utf-8")
    groups = text.split("export const SHORTCUT_GROUPS", 1)[1]
    groups = groups.split("export const Shortcuts", 1)[0]
    # Row objects hold no nested braces, so up to the first `}` is the object.
    return re.findall(r"\{ keys: \[([^\]]*)\]([^}]*)\}", groups)


def _handled_chips() -> set:
    """What the handler handles, as display chips.

    Keys with no entry in the table are dropped rather than raising: they are
    reported by test_the_table_cannot_silently_drop_a_key, which is the test
    that can explain them. Letting a KeyError escape here would bury that
    message under a traceback.
    """
    return {HANDLER_TO_DISPLAY[k] for k in _handler_keys() if k in HANDLER_TO_DISPLAY}


def _flagged_rows(flag: str) -> int:
    """How many rows opt out of the correspondence check, and why."""
    return sum(1 for _, rest in _rows() if f"{flag}: true" in rest)


def _documented_keys() -> set:
    """The chips the sheet advertises as player bindings, in display form.

    Rows flagged `gesture: true` or `system: true` are skipped: those are
    on-screen controls and shell bindings, not keys `setupKeys()` handles.

    Everything else counts whether or not this file knows the chip. That is
    deliberate - the comparison runs in display strings so a row advertising
    a key nobody implements has nowhere to hide. Dropping unknown chips here
    instead would turn exactly that bug into a passing test.
    """
    found = set()
    for chips, rest in _rows():
        if "gesture: true" in rest or "system: true" in rest:
            continue
        found.update(re.findall(r"'([^']+)'", chips))
    return found


class TestSheetMatchesTheHandler:
    def test_the_switch_is_where_the_test_looks(self):
        """If setupKeys moves, this file silently stops testing anything."""
        text = PLAYER.read_text(encoding="utf-8")
        assert "setupKeys() {" in text, "setupKeys moved; update this test"
        assert len(_handler_keys()) == 11, (
            f"expected the 11 documented bindings, found {sorted(_handler_keys())}"
        )

    def test_every_binding_is_documented(self):
        """The case that bites: a key is added and the sheet is not updated."""
        missing = _handled_chips() - _documented_keys()
        assert not missing, (
            f"player handles {sorted(missing)} but the shortcuts sheet does not "
            f"document them"
        )

    def test_nothing_is_documented_that_does_not_work(self):
        """The other direction: a binding is removed, the sheet still promises it."""
        phantom = _documented_keys() - _handled_chips()
        assert not phantom, (
            f"the shortcuts sheet advertises {sorted(phantom)}, which the "
            f"player no longer handles"
        )

    def test_the_table_cannot_silently_drop_a_key(self):
        """The hole this file had once.

        Both comparisons above run in display strings, so a handler key with
        no entry in DISPLAY_TO_HANDLER would fall out of the left-hand side
        unnoticed and the sheet would go unchecked for it. Pin the mapping.
        """
        unknown = _handler_keys() - set(DISPLAY_TO_HANDLER.values())
        assert not unknown, (
            f"player handles {sorted(unknown)}, which DISPLAY_TO_HANDLER does "
            f"not map to a display chip; add one so the sheet is checked for it"
        )

    def test_the_exclusion_flags_are_actually_used(self):
        """Flagging every row would empty the comparison and leave the two
        drift tests above vacuously true forever."""
        assert _flagged_rows("gesture") == 7, (
            "the mouse-gesture rows must stay flagged, or on-screen controls "
            "get checked against the player's key switch"
        )
        assert _flagged_rows("system") == 3, (
            "the shell bindings must stay flagged, or Ctrl+K / ? / Esc get "
            "reported as player shortcuts that do not exist"
        )


class TestSheetSuspendsThePlayer:
    def test_the_handler_checks_the_flag(self):
        text = PLAYER.read_text(encoding="utf-8")
        assert "suspended: false" in text, "Player.suspended is gone"
        body = text.split("setupKeys() {", 1)[1].split("switch (key)", 1)[0]
        assert "this.suspended" in body, (
            "the keydown handler no longer honours Player.suspended, so keys "
            "would leak through the open dialog"
        )

    def test_open_and_close_set_the_flag(self):
        text = SHEET.read_text(encoding="utf-8")
        opener = text.split("open() {", 1)[1].split("\n  },", 1)[0]
        closer = text.split("close() {", 1)[1].split("\n  },", 1)[0]
        assert "Player.suspended = true" in opener, "open() does not suspend the player"
        assert "Player.suspended = false" in closer, "close() does not release it"

    def test_the_flag_is_released_even_if_the_sheet_is_never_closed_by_hand(self):
        """Escape and the backdrop both route through close(), so there is one
        release path to get right."""
        text = SHEET.read_text(encoding="utf-8")
        assert text.count("Player.suspended = false") == 1, (
            "suspension is released in more than one place; it will drift"
        )


class TestSheetWiring:
    def test_the_sheet_exists_in_the_markup(self):
        html = HTML.read_text(encoding="utf-8")
        assert 'id="shortcuts-sheet"' in html
        assert 'id="shortcuts-body"' in html
        assert "data-open-shortcuts" in html, "nothing can open the sheet"

    def test_the_sheet_is_a_dialog(self):
        html = HTML.read_text(encoding="utf-8")
        head = html.split('id="shortcuts-sheet"', 1)[0].rsplit("<div", 1)[-1]
        tag = head + 'id="shortcuts-sheet"' + html.split('id="shortcuts-sheet"', 1)[1].split(">", 1)[0] + ">"
        assert 'role="dialog"' in tag and 'aria-modal="true"' in tag

    def test_the_hidden_rule_exists(self):
        """`.modal-backdrop` sets display:grid, which outranks the user-agent
        [hidden] rule. Without this the sheet is visible on page load."""
        css = CSS.read_text(encoding="utf-8")
        assert ".modal-backdrop[hidden] { display: none; }" in css, (
            "the sheet will render on page load without this rule"
        )

    def test_keys_and_gestures_are_visually_distinct(self):
        """A key cap and a button must not look like the same thing to press."""
        css = CSS.read_text(encoding="utf-8")
        assert ".sc-key {" in css and ".sc-gesture {" in css


class TestContentAccuracy:
    """The claims the sheet makes about the player's own behaviour."""

    def test_the_seek_step_is_five_seconds(self):
        player = PLAYER.read_text(encoding="utf-8")
        step = re.search(r"const SEEK_STEP = ([\d.]+)", player).group(1)
        assert step == "5", f"the sheet says 5 seconds; SEEK_STEP is {step}"

    def test_the_clear_queue_row_is_a_real_control(self):
        html = HTML.read_text(encoding="utf-8")
        assert 'id="pq-clear"' in html, "the sheet advertises a button that is not there"

    def test_the_gesture_rows_are_the_ones_library_actually_binds(self):
        """Shift-click play-from-here and play-next are the least discoverable
        part of the player, so the sheet claiming them has to be true."""
        lib = (ROOT / "frontend" / "js" / "tabs" / "library.js").read_text(encoding="utf-8")
        assert "e.shiftKey" in lib
        assert "Player.playNext(" in lib
        assert "Player.enqueue(" in lib

    def test_the_queue_row_gestures_are_the_ones_the_panel_binds(self):
        """The queue panel's own two controls. These live in player.js rather
        than library.js, so the check above cannot see them - and they are the
        two least discoverable controls in the whole player."""
        panel = PLAYER.read_text(encoding="utf-8")
        panel = panel.split("renderQueue() {", 1)[1].split("\n  },", 1)[0]
        assert "data-remove" in panel, "the per-row remove button is gone"
        assert "this.removeAt(" in panel, "the remove button no longer removes"
        assert "row.addEventListener('click'" in panel, (
            "queue rows are no longer clickable, but the sheet says clicking one "
            "plays it"
        )
