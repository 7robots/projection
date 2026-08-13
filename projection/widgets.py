"""Custom widgets: project rows, sidebar option builders, view header."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import ListItem, ListView, ProgressBar, Static
from textual.widgets.option_list import Option

from .config import DONE_STATUS, status_color, status_icon
from .models import Project, parse_stamp

COLOR_ALL = "#0A84FF"
COLOR_STARRED = "#BF5AF2"
COLOR_OVERDUE = "#FF453A"
COLOR_STAR_BADGE = "#64D2FF"
# Conflicts are the one thing on a row that needs acting on, so they get the
# warning colour rather than an accent.
COLOR_CONFLICT = "#FF9F0A"

# Statuses that count as "done" for dimming and the header progress bar.
DONE_STATUSES = (DONE_STATUS,)


@dataclass(frozen=True)
class SmartView:
    """A built-in virtual view over the local project cache."""

    key: str
    label: str
    icon: str
    color: str
    empty: str


SMART_VIEWS = (
    SmartView("all", "All Projects", "▦", COLOR_ALL, "no projects — press n to add one"),
    SmartView("starred", "Starred", "⟳", COLOR_STARRED, "no starred projects — press s to star one"),
    # Shown in the sidebar only while it has something in it -- see
    # ProjectsPanel._nav_views. A permanent row reading 0 would be clutter; a row
    # that appears when a colleague's edit collides with yours is a signal.
    SmartView("conflicts", "Conflicts", "⚠", COLOR_CONFLICT, "no conflicts"),
)

# Smart views hidden from the sidebar when they are empty.
TRANSIENT_VIEWS = ("conflicts",)


# Classic Apple logo rainbow, matching the remtui look.
APPLE_RAINBOW = ("#61BB46", "#FDB827", "#F5821F", "#E03A3E", "#963D97", "#009DDC")

# "Projection" in box-drawing characters, one glyph per letter.
_WORDMARK = (
    ("┌─┐", "├─┘", "┴  "),  # P
    ("┬─┐", "├┬┘", "┴└─"),  # R
    ("┌─┐", "│ │", "└─┘"),  # O
    (" ┬", " │", "└┘"),     # J
    ("┌─", "├─", "└─"),     # E
    ("┌─", "│ ", "└─"),     # C
    ("┌┬┐", " │ ", " ┴ "),  # T
    ("┬", "│", "┴"),        # I
    ("┌─┐", "│ │", "└─┘"),  # O
    ("┌┐┌", "│││", "┘└┘"),  # N
)


def logo() -> Text:
    """The sidebar logo: the Projection wordmark in Apple-rainbow colors."""
    out = Text()
    for row in range(3):
        if row:
            out.append("\n")
        for index, glyph in enumerate(_WORDMARK):
            color = APPLE_RAINBOW[index % len(APPLE_RAINBOW)]
            out.append(glyph[row], style=f"bold {color}")
    return out


def sync_age_seconds(iso: str | None) -> float | None:
    """Seconds since an ISO timestamp, or None if missing/unparseable.

    Both sides of the subtraction are timezone-aware. A bare `datetime.now()`
    here worked only while every stored stamp was naive too; once sync
    timestamps became aware it raised `TypeError` inside the repopulate worker,
    which takes the panel down rather than showing a wrong age.
    """
    then = parse_stamp(iso)
    if then is None:
        return None
    return (datetime.now(timezone.utc) - then).total_seconds()


def humanize_age(iso: str | None) -> str:
    """A compact human age for an ISO timestamp ('just now', '5m ago', …)."""
    seconds = sync_age_seconds(iso)
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 2 * 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def parse_due(due: str) -> date | None:
    """Parse a sheet due date (M/D/YYYY, with a few tolerated variants)."""
    due = (due or "").strip()
    if not due:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(due, fmt).date()
        except ValueError:
            continue
    return None


def is_overdue(project: Project) -> bool:
    parsed = parse_due(project.due_date)
    return (
        parsed is not None
        and parsed < date.today()
        and project.status not in DONE_STATUSES
    )


class ProjectItem(ListItem):
    """One project row: status glyph | title + full update text | status + due.

    The status update is the point of the row — it renders in full, wrapped,
    in the normal foreground color, while everything else stays muted.
    """

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self.overdue = is_overdue(project)
        if project.status in DONE_STATUSES:
            self.add_class("-done")

    def compose(self) -> ComposeResult:
        yield Static(self._check(), classes="check")
        yield Static(self._body(), classes="body")
        lines = self._meta_lines()
        meta = Text("\n").join(lines)
        meta.justify = "right"
        meta.no_wrap = True
        meta_static = Static(meta, classes="meta")
        # Horizontal layout gives the 1fr body the remainder only after
        # fixed widths resolve; pin the meta cell to its rendered width so
        # the status label never wraps or crops.
        meta_static.styles.width = max((line.cell_len for line in lines), default=1)
        yield meta_static

    def _check(self) -> Text:
        status = self.project.status
        color = COLOR_OVERDUE if self.overdue else status_color(status)
        return Text(status_icon(status), style=color)

    def _body(self) -> Text:
        p = self.project
        body = Text()
        body.append(p.title, style="bold")
        if p.is_starred:
            body.append(" ⟳", style=COLOR_STAR_BADGE)
        if p.has_conflicts:
            body.append(f" ⚠{len(p.conflicts)}", style=f"bold {COLOR_CONFLICT}")
        if p.assigned_str:
            body.append(f"  · {p.assigned_str}", style="dim italic")
        body.append("\n")
        if p.note_text:
            body.append(p.note_text)
        else:
            body.append("(no update)", style="dim italic")
        return body

    def _meta_lines(self) -> list[Text]:
        p = self.project
        lines = [Text(p.status, style=f"bold {status_color(p.status)}")]
        if p.due_date:
            style = f"bold {COLOR_OVERDUE}" if self.overdue else "dim"
            lines.append(Text(p.due_date, style=style))
        return lines


class ProjectList(ListView):
    """ListView whose paging keys move the selection, not just the viewport.

    Stock ListView binds only up/down/enter for the cursor; PageUp/PageDown/
    Home/End fall through to the scroll container and scroll the viewport
    while the highlight stays behind. These overrides keep the selection in
    step, matching the sidebar OptionList's behavior.
    """

    BINDINGS = [
        Binding("pageup", "cursor_page_up", "Page up", show=False),
        Binding("pagedown", "cursor_page_down", "Page down", show=False),
        Binding("home", "cursor_home", "First", show=False),
        Binding("end", "cursor_end", "Last", show=False),
    ]

    def action_cursor_home(self) -> None:
        if len(self):
            self.index = 0

    def action_cursor_end(self) -> None:
        if len(self):
            self.index = len(self) - 1

    def action_cursor_page_up(self) -> None:
        self.cursor_page(-1, 1.0)

    def action_cursor_page_down(self) -> None:
        self.cursor_page(1, 1.0)

    def cursor_page(self, direction: int, fraction: float) -> None:
        """Move the selection by (a fraction of) one viewport of items.

        Items have variable height, so advance until their accumulated
        height fills the requested share of the viewport.
        """
        if not len(self):
            return
        budget = max(1, int(self.scrollable_content_region.height * fraction))
        index = self.index or 0
        children = list(self.children)
        while 0 <= index + direction < len(children) and budget > 0:
            index += direction
            budget -= max(1, children[index].outer_size.height)
        self.index = index


def nav_header(label: str) -> Option:
    """A non-selectable section heading for the sidebar."""
    return Option(Text(label.upper(), style="bold #6E6E73"), disabled=True)


def _count_option(
    icon: str, color: str, label: str, count: int, option_id: str
) -> Option:
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right")
    left = Text()
    left.append(f"{icon} ", style=color)
    left.append(label)
    right = Text(str(count) if count else "", style="dim")
    grid.add_row(left, right)
    return Option(grid, id=option_id)


def smart_option(view: SmartView, count: int) -> Option:
    return _count_option(view.icon, view.color, view.label, count, f"view:{view.key}")


def status_option(status: str, count: int) -> Option:
    return _count_option(
        status_icon(status), status_color(status), status, count, f"status:{status}"
    )


class ViewHeader(Widget):
    """Header strip above the project list: view name, counts, progress."""

    DEFAULT_CSS = """
    ViewHeader {
        height: auto;
        padding: 1 2 0 2;
    }
    ViewHeader #vh-title { text-style: bold; }
    ViewHeader #vh-stats { color: $text-muted; }
    ViewHeader #vh-bar { width: 32; height: 1; display: none; }
    ViewHeader #vh-bar.-visible { display: block; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="vh-title")
        yield Static("", id="vh-stats")
        yield ProgressBar(id="vh-bar", show_eta=False)

    def show_view(
        self,
        *,
        label: str,
        icon: str,
        color: str,
        shown: int,
        active: int | None = None,
        completed: int | None = None,
        filter_text: str = "",
        sync_note: Text | None = None,
    ) -> None:
        title = Text()
        title.append(f"{icon} ", style=color)
        title.append(label, style=f"bold {color}")
        self.query_one("#vh-title", Static).update(title)

        parts: list[str] = []
        if active is not None:
            parts.append(f"{active} active")
        if completed:
            parts.append(f"{completed} done")
        if active is None:
            parts.append(f"{shown} project{'s' if shown != 1 else ''}")
        if filter_text:
            parts.append(f'filter "{filter_text}" → {shown} match{"es" if shown != 1 else ""}')
        stats = Text(" · ".join(parts))
        if sync_note is not None:
            stats.append("  ")
            stats.append(sync_note)
        self.query_one("#vh-stats", Static).update(stats)

        bar = self.query_one("#vh-bar", ProgressBar)
        total = (active or 0) + (completed or 0)
        if completed is not None and total > 0:
            bar.add_class("-visible")
            bar.update(total=total, progress=completed)
        else:
            bar.remove_class("-visible")
