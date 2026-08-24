"""Reusable keyboard-first dropdown used throughout the terminal UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Iterable

from rich.text import Text
from textual.events import Blur, Focus, Key
from textual.message import Message
from textual.widgets import Static

from theme_palette import THEME_PALETTES


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
    ) -> None:
        super().__init__(id=widget_id, classes="keyboard-dropdown", markup=False)
        self.setting_name = name
        self.label = label
        self.prefix = prefix
        self.suffix = suffix
        self.options = tuple(options)
        self.value = value
        self.is_open = False
        self._highlighted_index = self._selected_index()
        self._focused = False
        default_palette = THEME_PALETTES["white"]
        self._base_color = default_palette.base
        self._accent_color = default_palette.accent
        self._subdued_color = default_palette.dim_base

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

    def set_palette(self, base: str, accent: str, subdued: str) -> None:
        """Apply the app's current semantic BASE/ACCENT palette immediately."""
        self._base_color = base
        self._accent_color = accent
        self._subdued_color = subdued
        self._render_dropdown()

    def open_menu(self) -> None:
        if not self.options:
            return
        self.is_open = True
        self._highlighted_index = self._selected_index()
        self.add_class("open")
        self._render_dropdown()

    def close_menu(self) -> None:
        self.is_open = False
        self.remove_class("open")
        self._render_dropdown()

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
            self._render_dropdown()
            event.stop()
        elif event.key == "enter":
            option = self.options[self._highlighted_index]
            self.value = option.value
            self.close_menu()
            self.post_message(self.Selected(self, option.value))
            event.stop()
        elif event.key == "escape":
            self.close_menu()
            event.stop()

    def on_focus(self, _event: Focus) -> None:
        self._focused = True
        self._render_dropdown()

    def on_blur(self, _event: Blur) -> None:
        self._focused = False
        if self.is_open:
            self.close_menu()
        else:
            self._render_dropdown()

    def _selected_index(self) -> int:
        return next(
            (
                index
                for index, option in enumerate(self.options)
                if option.value == self.value
            ),
            0,
        )

    def _render_dropdown(self) -> None:
        marker = ">" if self._focused else " "
        heading = f"{marker} {self.prefix}{self.label} [ {self.selected_label} ▾ ]"
        if self.suffix:
            heading += f" {self.suffix}"
        text = Text(heading, style=self._base_color)
        if not self.is_open:
            self.update(text)
            return

        width = max(len(option.label) for option in self.options) + 5
        text.append(f"\n  ┌{'─' * width}┐", style=self._subdued_color)
        for index, option in enumerate(self.options):
            current = "◀" if option.value == self.value else " "
            line_start = len(text)
            text.append(
                f"\n  │ {option.label:<{width - 3}} {current} │",
                style=self._base_color,
            )
            if current.strip():
                text.stylize(self._accent_color, len(text) - 3, len(text) - 2)
            if index == self._highlighted_index:
                text.stylize(
                    f"bold {self._accent_color}",
                    line_start + 4,
                    len(text) - 2,
                )
        text.append(f"\n  └{'─' * width}┘", style=self._subdued_color)
        self.update(text)
