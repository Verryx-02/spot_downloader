"""
Progress bar handling for spot-downloader using Rich library.

This module provides styled progress bars for all phases of the download workflow.
All progress bars share a common base class and spotDL-style theming.

Phases:
    - Phase 1 (Spotify fetch): No progress bar needed (operation too fast)
    - Phase 2 (YouTube matching): MatchingProgressBar
    - Phase 3 (Download): DownloadProgressBar
    - Phase 4 (Lyrics): LyricsProgressBar
    - Phase 5 (Metadata): MetadataProgressBar

Usage:
    from spot_downloader.core.progress import MatchingProgressBar, DownloadProgressBar
    
    # As context manager
    with DownloadProgressBar(total=100) as progress:
        for item in items:
            success = process(item)
            progress.update(success=success)
    
    # Manual control
    progress = MatchingProgressBar(total=50)
    progress.start()
    # ... do work with progress.update() ...
    progress.stop()
"""

from abc import ABC, abstractmethod
from typing import Optional

from rich import get_console
from rich.console import JustifyMethod, OverflowMethod
from rich.highlighter import Highlighter
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    Task,
    TaskID,
)
from rich.style import StyleType
from rich.text import Text
from rich.theme import Theme


# =============================================================================
# Common Theme (spotDL-style colors)
# =============================================================================

PROGRESS_THEME = Theme({
    "bar.back": "grey23",
    "bar.complete": "rgb(165,66,129)",  # Magenta/purple
    "bar.finished": "rgb(114,156,31)",  # Green when done
    "bar.pulse": "rgb(165,66,129)",
    "progress.percentage": "white",
})


# =============================================================================
# Custom Column
# =============================================================================

class SizedTextColumn(ProgressColumn):
    """
    Custom sized text column based on the Rich library.
    
    Allows text to be truncated with ellipsis if it exceeds
    the specified width.
    """

    def __init__(
        self,
        text_format: str,
        style: StyleType = "none",
        justify: JustifyMethod = "left",
        markup: bool = True,
        highlighter: Optional[Highlighter] = None,
        overflow: Optional[OverflowMethod] = None,
        width: int = 20,
    ) -> None:
        """
        Initialize the sized text column.
        
        Args:
            text_format: Format string for the text.
            style: Style to apply.
            justify: Text justification.
            markup: Whether to parse markup.
            highlighter: Optional highlighter.
            overflow: Overflow handling method.
            width: Maximum width of the column.
        """
        self.text_format = text_format
        self.justify: JustifyMethod = justify
        self.style = style
        self.markup = markup
        self.highlighter = highlighter
        self.overflow: Optional[OverflowMethod] = overflow
        self.width = width
        super().__init__()

    def render(self, task: Task) -> Text:
        """Render the column."""
        _text = self.text_format.format(task=task)
        if self.markup:
            text = Text.from_markup(_text, style=self.style, justify=self.justify)
        else:
            text = Text(_text, style=self.style, justify=self.justify)
        if self.highlighter:
            self.highlighter.highlight(text)

        text.truncate(max_width=self.width, overflow=self.overflow, pad=True)
        return text


# =============================================================================
# Base Progress Bar
# =============================================================================

class BaseProgressBar(ABC):
    """
    Abstract base class for all progress bars.
    
    Provides common functionality:
    - Rich Progress instance with spotDL-style theming
    - Context manager support (__enter__/__exit__)
    - Manual start/stop control
    - Log method for printing above the progress bar
    
    Subclasses must implement:
    - _get_status_text(): Return formatted status string
    - update(): Update progress with phase-specific logic
    """
    
    def __init__(
        self,
        total: int,
        description: str,
        status_width: int = 35
    ):
        """
        Initialize the progress bar.
        
        Args:
            total: Total number of items to process.
            description: Description to show on the left (e.g., "Matching").
            status_width: Width of the status column.
        """
        self.total = total
        self.description = description
        self.completed = 0
        
        self.console = get_console()
        self.console.push_theme(PROGRESS_THEME)
        
        self.progress = Progress(
            SizedTextColumn(
                "[white]{task.description}",
                overflow="ellipsis",
                width=15,
            ),
            SizedTextColumn(
                "{task.fields[status]}",
                width=status_width,
                style="white",
            ),
            BarColumn(bar_width=40, finished_style="green"),
            "[progress.percentage]{task.percentage:>3.0f}%",
            console=self.console,
            transient=False,
            refresh_per_second=10,
        )
        
        self.task_id: Optional[TaskID] = None
        self._started = False
    
    def __enter__(self) -> "BaseProgressBar":
        """Start the progress bar."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Stop the progress bar."""
        self.stop()
    
    def start(self) -> None:
        """Start the progress bar (can be called manually)."""
        if not self._started:
            self.progress.start()
            self.task_id = self.progress.add_task(
                description=self.description,
                total=self.total,
                status=self._get_status_text(),
            )
            self._started = True
    
    def stop(self) -> None:
        """Stop the progress bar."""
        if self._started:
            self.progress.stop()
            self._started = False
    
    def log(self, message: str) -> None:
        """
        Print a log message above the progress bar.
        
        Args:
            message: The message to print.
        """
        self.progress.console.print(message, highlight=False)
    
    def _update_progress(self) -> None:
        """Update the Rich progress bar with current state."""
        if self.task_id is not None:
            self.progress.update(
                self.task_id,
                completed=self.completed,
                status=self._get_status_text(),
            )
    
    @abstractmethod
    def _get_status_text(self) -> str:
        """
        Get the status text for the progress bar.
        
        Returns:
            Formatted status string with Rich markup.
        """
        pass
    
    @abstractmethod
    def update(self, *args, **kwargs) -> None:
        """
        Update the progress bar with a completed item.
        
        Signature varies by phase.
        """
        pass


# =============================================================================
# Phase 2: Matching Progress Bar
# =============================================================================

class MatchingProgressBar(BaseProgressBar):
    """
    Progress bar for YouTube matching phase (Phase 2).
    
    Displays:
    - Description (e.g., "Matching")
    - Status: ✓ matched, ✗ failed, ⚠ close matches
    - Progress bar with spotDL-style colors
    - Percentage
    
    Example:
        Matching        ✓ 45  ✗ 2  ⚠ 3         ━━━━━━━━━━━━━━━━━  47%
    """
    
    def __init__(self, total: int, description: str = "Matching"):
        """
        Initialize the matching progress bar.
        
        Args:
            total: Total number of tracks to match.
            description: Description to show on the left.
        """
        super().__init__(total=total, description=description)
        self.matched = 0
        self.failed = 0
        self.close_matches = 0
    
    def _get_status_text(self) -> str:
        """Get status showing matched/failed/close matches counts."""
        parts = [
            f"[green]✓ {self.matched}[/green]",
            f"[red]✗ {self.failed}[/red]",
        ]
        if self.close_matches > 0:
            parts.append(f"[yellow]⚠ {self.close_matches}[/yellow]")
        return "  ".join(parts)
    
    def update(self, matched: bool, has_close_matches: bool = False) -> None:
        """
        Update the progress bar with a completed match.
        
        Args:
            matched: Whether the track was successfully matched.
            has_close_matches: Whether the match has close alternatives.
        """
        self.completed += 1
        if matched:
            self.matched += 1
            if has_close_matches:
                self.close_matches += 1
        else:
            self.failed += 1
        
        self._update_progress()


# =============================================================================
# Phase 3: Download Progress Bar
# =============================================================================

class DownloadProgressBar(BaseProgressBar):
    """
    Progress bar for download phase (Phase 3).
    
    Displays:
    - Description (e.g., "Downloading")
    - Status: ✓ downloaded, ✗ failed, ⊘ skipped
    - Progress bar with spotDL-style colors
    - Percentage
    
    Example:
        Downloading     ✓ 120  ✗ 3  ⊘ 5        ━━━━━━━━━━━━━━━━━  64%
    """
    
    def __init__(self, total: int, description: str = "Downloading"):
        """
        Initialize the download progress bar.
        
        Args:
            total: Total number of tracks to download.
            description: Description to show on the left.
        """
        super().__init__(total=total, description=description)
        self.downloaded = 0
        self.failed = 0
        self.skipped = 0
    
    def _get_status_text(self) -> str:
        """Get status showing downloaded/failed/skipped counts."""
        parts = [
            f"[green]✓ {self.downloaded}[/green]",
            f"[red]✗ {self.failed}[/red]",
        ]
        if self.skipped > 0:
            parts.append(f"[yellow]⊘ {self.skipped}[/yellow]")
        return "  ".join(parts)
    
    def update(self, success: bool, skipped: bool = False) -> None:
        """
        Update the progress bar with a completed download.
        
        Args:
            success: Whether the download succeeded.
            skipped: Whether the track was skipped (already exists).
        """
        self.completed += 1
        if skipped:
            self.skipped += 1
        elif success:
            self.downloaded += 1
        else:
            self.failed += 1
        
        self._update_progress()


# =============================================================================
# Phase 4: Lyrics Progress Bar
# =============================================================================

class LyricsProgressBar(BaseProgressBar):
    """
    Progress bar for lyrics fetching phase (Phase 4).
    
    Displays:
    - Description (e.g., "Lyrics")
    - Status: ✓ found, ✗ not found, ♪ synced
    - Progress bar with spotDL-style colors
    - Percentage
    
    Example:
        Lyrics          ✓ 80  ✗ 15  ♪ 45       ━━━━━━━━━━━━━━━━━  95%
    """
    
    def __init__(self, total: int, description: str = "Lyrics"):
        """
        Initialize the lyrics progress bar.
        
        Args:
            total: Total number of tracks to fetch lyrics for.
            description: Description to show on the left.
        """
        super().__init__(total=total, description=description)
        self.found = 0
        self.not_found = 0
        self.synced = 0  # Tracks with synced (timed) lyrics
    
    def _get_status_text(self) -> str:
        """Get status showing found/not found/synced counts."""
        parts = [
            f"[green]✓ {self.found}[/green]",
            f"[red]✗ {self.not_found}[/red]",
        ]
        if self.synced > 0:
            parts.append(f"[cyan]♪ {self.synced}[/cyan]")
        return "  ".join(parts)
    
    def update(self, found: bool, synced: bool = False) -> None:
        """
        Update the progress bar with a completed lyrics fetch.
        
        Args:
            found: Whether lyrics were found.
            synced: Whether the lyrics are synced (timed).
        """
        self.completed += 1
        if found:
            self.found += 1
            if synced:
                self.synced += 1
        else:
            self.not_found += 1
        
        self._update_progress()


# =============================================================================
# Phase 5: Metadata Progress Bar
# =============================================================================

class MetadataProgressBar(BaseProgressBar):
    """
    Progress bar for metadata embedding phase (Phase 5).
    
    Displays:
    - Description (e.g., "Metadata")
    - Status: ✓ embedded, ✗ failed, ♪ with lyrics
    - Progress bar with spotDL-style colors
    - Percentage
    
    Example:
        Metadata        ✓ 150  ✗ 2  ♪ 120      ━━━━━━━━━━━━━━━━━  76%
    """
    
    def __init__(self, total: int, description: str = "Metadata"):
        """
        Initialize the metadata progress bar.
        
        Args:
            total: Total number of tracks to embed metadata for.
            description: Description to show on the left.
        """
        super().__init__(total=total, description=description)
        self.embedded = 0
        self.failed = 0
        self.with_lyrics = 0  # Tracks where lyrics were also embedded
    
    def _get_status_text(self) -> str:
        """Get status showing embedded/failed/with_lyrics counts."""
        parts = [
            f"[green]✓ {self.embedded}[/green]",
            f"[red]✗ {self.failed}[/red]",
        ]
        if self.with_lyrics > 0:
            parts.append(f"[cyan]♪ {self.with_lyrics}[/cyan]")
        return "  ".join(parts)
    
    def update(self, success: bool, with_lyrics: bool = False) -> None:
        """
        Update the progress bar with a completed metadata embed.
        
        Args:
            success: Whether the metadata was successfully embedded.
            with_lyrics: Whether lyrics were also embedded.
        """
        self.completed += 1
        if success:
            self.embedded += 1
            if with_lyrics:
                self.with_lyrics += 1
        else:
            self.failed += 1
        
        self._update_progress()


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Theme and utilities
    "PROGRESS_THEME",
    "SizedTextColumn",
    # Base class
    "BaseProgressBar",
    # Phase-specific progress bars
    "MatchingProgressBar",   # Phase 2
    "DownloadProgressBar",   # Phase 3
    "LyricsProgressBar",     # Phase 4
    "MetadataProgressBar",   # Phase 5
]
