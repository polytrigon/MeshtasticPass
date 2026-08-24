"""Shared viewport-aware popup menus for keyboard-first terminal controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Hashable, Iterable

from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Click
from textual.geometry import Region
from textual.widgets import Static


@dataclass(frozen=True)
class PopupItem:
    """One popup row; informational rows are intentionally not actionable."""

    label: str
    value: Hashable | None = None
    actionable: bool = True


@dataclass(frozen=True)
class PopupPlacement:
    direction: str
    x: int
    y: int
    width: int
    height: int
    constrained: bool


def calculate_popup_placement(
    anchor: Region,
    viewport: Region,
    desired_width: int,
    desired_height: int,
) -> PopupPlacement:
    """Place a popup in the actual visible viewport, preferring below."""
    width = max(1, min(desired_width, viewport.width))
    below = max(0, viewport.bottom - anchor.bottom)
    above = max(0, anchor.y - viewport.y)

    if desired_height <= below:
        direction = "down"
        height = desired_height
        y = anchor.bottom
        constrained = False
    elif desired_height <= above:
        direction = "up"
        height = desired_height
        y = anchor.y - height
        constrained = False
    else:
        direction = "down" if below >= above else "up"
        usable = below if direction == "down" else above
        height = max(1, min(desired_height, usable or viewport.height))
        y = anchor.bottom if direction == "down" else anchor.y - height
        constrained = height < desired_height

    x = min(max(anchor.x, viewport.x), max(viewport.x, viewport.right - width))
    y = min(max(y, viewport.y), max(viewport.y, viewport.bottom - height))
    return PopupPlacement(direction, x, y, width, height, constrained)


class PopupMenuRow(Static):
    """Mouse-selectable row inside a shared popup."""

    can_focus = False

    def __init__(self, item: PopupItem, index: int) -> None:
        self.item = item
        self.index = index
        classes = "viewport-menu-row"
        if not item.actionable:
            classes += " informational"
        super().__init__(item.label, classes=classes, markup=False)

    @on(Click)
    def clicked(self) -> None:
        if self.item.actionable:
            popup = self.parent
            if isinstance(popup, ViewportMenu):
                popup.activate(self.index)


class ViewportMenu(VerticalScroll):
    """Overlay menu with bounded height and internally scrolling rows."""

    can_focus = False

    def __init__(
        self,
        items: Iterable[PopupItem],
        *,
        highlighted_index: int,
        on_activate: Callable[[int, PopupItem], None],
        menu_id: str | None = None,
    ) -> None:
        super().__init__(id=menu_id, classes="viewport-menu")
        self.items = tuple(items)
        self.highlighted_index = highlighted_index
        self._on_activate = on_activate
        self.placement: PopupPlacement | None = None

    def compose(self) -> ComposeResult:
        for index, item in enumerate(self.items):
            yield PopupMenuRow(item, index)

    def place(self, anchor: Region, viewport: Region, width: int) -> None:
        placement = calculate_popup_placement(
            anchor,
            viewport,
            width,
            len(self.items) + 2,
        )
        self.placement = placement
        self.styles.width = placement.width
        self.styles.height = placement.height
        self.styles.offset = (placement.x, placement.y)

    def on_mount(self) -> None:
        self.set_highlight(self.highlighted_index)

    def set_highlight(self, index: int) -> None:
        self.highlighted_index = index
        rows = list(self.query(PopupMenuRow))
        for row in rows:
            row.set_class(row.index == index, "highlighted")
        if 0 <= index < len(rows):
            self.call_after_refresh(rows[index].scroll_visible, animate=False)

    def move_highlight(self, direction: int) -> None:
        """Move cyclically among actionable rows, skipping information."""
        actionable = [
            index for index, item in enumerate(self.items) if item.actionable
        ]
        if not actionable:
            return
        try:
            position = actionable.index(self.highlighted_index)
        except ValueError:
            position = 0
        self.set_highlight(actionable[(position + direction) % len(actionable)])

    def activate(self, index: int | None = None) -> None:
        chosen = self.highlighted_index if index is None else index
        if not 0 <= chosen < len(self.items):
            return
        item = self.items[chosen]
        if item.actionable:
            self._on_activate(chosen, item)
