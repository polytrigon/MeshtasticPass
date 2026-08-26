"""Reusable keyboard-first dropdown used throughout the terminal UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Iterable

from rich.text import Text
from textual import on
from textual.events import Blur, Click, Focus, Key
from textual.message import Message
from textual.widgets import Static

from theme_palette import THEME_PALETTES
from viewport_menu import PopupItem, ViewportMenu


@dataclass(frozen=True)
class DropdownOption:
    """One visible label and stable application value."""

    label: str
    value: Hashable


class KeyboardDropdown(Static):
    """A compact dropdown that is fully usable without a mouse."""

    can_focus = True

    class Selected(Message):
        def __init__(self, dropdown: "KeyboardDropdown", value: Hashable) -> None:
            super().__init__()
            self.dropdown = dropdown
            self.setting_name = dropdown.setting_name
            self.value = value

    def __init__(
        self,
        name: str,
        label: str,
        options: Iterable[DropdownOption],
        value: Hashable,
        *,
        widget_id: str,
        prefix: str = "",
        suffix: str = "",
        label_width: int | None = None,
        classes: str = "keyboard-dropdown",
    ) -> None:
        super().__init__(id=widget_id, classes=classes, markup=False)
        self.setting_name = name
        self.label = label
        self.prefix = prefix
        self.suffix = suffix
        self.label_width = label_width
        self.options = tuple(options)
        self.value = value
        self.is_open = False
        self._status_override: str | None = None
        self._status_override_color: str | None = None
        self._highlighted_index = self._selected_index()
        default_palette = THEME_PALETTES["white"]
        self._base_color = default_palette.base
        self._accent_color = default_palette.accent
        self._subdued_color = default_palette.dim_base
        self.popup: ViewportMenu | None = None

    @property
    def selected_label(self) -> str:
        for option in self.options:
            if option.value == self.value:
                return option.label
        return str(self.value)

    def on_mount(self) -> None:
        self._render_dropdown()

    def set_options(
        self,
        options: Iterable[DropdownOption],
        *,
        value: Hashable | None = None,
    ) -> None:
        self.options = tuple(options)
        if value is not None:
            self.value = value
        self._highlighted_index = self._selected_index()
        self._render_dropdown()

    def set_suffix(self, suffix: str) -> None:
        self.suffix = suffix
        self._render_dropdown()

    def set_status_override(self, text: str | None, *, color: str | None = None) -> None:
        """Temporarily replace this dropdown's normal heading with a

        plain, non-interactive status line (e.g. "STATUS CONNECTING...")
        -- used while the underlying value can't meaningfully be shown
        or changed. `value`/`options` are never touched, so the normal
        heading (built from them) reappears exactly as it was the
        moment `None` is passed to restore it. Also closes and blocks
        the dropdown menu while overridden, so a stray open menu or
        keypress can never edit a setting the UI is telling the user is
        currently unavailable.

        `color`, when given, styles this status text with that semantic
        color instead of the ordinary BASE this widget's other content
        uses -- e.g. the caller's own shared connection-status color
        (see MeshtasticPassApp._connection_status_color), so this status
        text visually agrees with wherever else that same state is
        shown. Ignored once `text` is None -- there's no override text
        left for it to color.
        """
        self._status_override = text
        self._status_override_color = color
        self.disabled = text is not None
        if text is not None:
            self.close_menu()
        else:
            self._render_dropdown()

    def set_palette(self, base: str, accent: str, subdued: str) -> None:
        """Apply the app's current semantic BASE/ACCENT palette immediately."""
        self._base_color = base
        self._accent_color = accent
        self._subdued_color = subdued
        self._render_dropdown()

    def open_menu(self) -> None:
        if not self.options or self._status_override is not None:
            return
        self.is_open = True
        self._highlighted_index = self._selected_index()
        self.add_class("open")
        items = tuple(PopupItem(option.label, option.value) for option in self.options)
        self.popup = ViewportMenu(
            items,
            highlighted_index=self._highlighted_index,
            on_activate=self._activate_popup_item,
        )
        width = max(len(option.label) for option in self.options) + 4
        self.screen.mount(self.popup)
        self.popup.place(self.region, self.screen.region, width)
        self._render_dropdown()

    def close_menu(self) -> None:
        self.is_open = False
        self.remove_class("open")
        if self.popup is not None:
            self.popup.remove()
            self.popup = None
        self._render_dropdown()

    def _activate_popup_item(self, index: int, _item: PopupItem) -> None:
        self._highlighted_index = index
        option = self.options[index]
        self.value = option.value
        self.close_menu()
        self.post_message(self.Selected(self, option.value))

    def on_key(self, event: Key) -> None:
        if not self.is_open:
            if event.key == "enter":
                self.open_menu()
                event.stop()
            return

        if event.key in ("up", "down"):
            direction = -1 if event.key == "up" else 1
            self._highlighted_index = (
                self._highlighted_index + direction
            ) % len(self.options)
            if self.popup is not None:
                self.popup.set_highlight(self._highlighted_index)
            self._render_dropdown()
            event.stop()
        elif event.key == "enter":
            self._activate_popup_item(
                self._highlighted_index,
                PopupItem(self.options[self._highlighted_index].label),
            )
            event.stop()
        elif event.key == "escape":
            self.close_menu()
            event.stop()

    def on_focus(self, _event: Focus) -> None:
        self._render_dropdown(focused=True)

    def on_blur(self, _event: Blur) -> None:
        if self.is_open:
            self.close_menu()
        self._render_dropdown(focused=False)

    @on(Click)
    def clicked(self) -> None:
        if self.is_open:
            self.close_menu()
        else:
            self.open_menu()

    def _selected_index(self) -> int:
        return next(
            (
                index
                for index, option in enumerate(self.options)
                if option.value == self.value
            ),
            0,
        )

    def _render_dropdown(self, *, focused: bool | None = None) -> None:
        if self._status_override is not None:
            self.update(
                Text(
                    self._status_override,
                    style=self._status_override_color or self._base_color,
                )
            )
            return
        marker = ">" if (self.has_focus if focused is None else focused) else " "
        label = f"{self.prefix}{self.label}"
        if self.label_width is not None:
            label = f"{label:<{self.label_width}}"
            heading = f"{marker} {label} [ {self.selected_label} ▾ ]"
        else:
            heading = f"{marker} {label} [ {self.selected_label} ▾ ]"
        if self.suffix:
            heading += f" {self.suffix}"
        text = Text(heading, style=self._base_color)
        if not self.is_open:
            self.update(text)
            return

        self.update(text)
