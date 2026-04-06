import time
import logging
from pathlib import Path
from typing import Set

logger = logging.getLogger("cerebro.pipeline")


class ChangeClassifier:
    """
    Distinguishes user changes from internal Skrymir changes.

    Uses a marker file approach: creates temporary marker before internal changes,
    removes after. Also tracks recently fixed files.
    """

    MARKER_FILE = ".skrymir-internal"
    FIX_COOLDOWN_SECONDS = 10  # Consider recent fixes as internal

    def __init__(self):
        self._recent_fixes: dict[str, float] = {}  # file_path -> timestamp

    def mark_as_internal(self, file_paths: list[str]) -> None:
        """Mark files as being modified by Skrymir internally."""
        timestamp = time.time()
        for path in file_paths:
            self._recent_fixes[path] = timestamp
            self._create_marker_file(path)

    def is_internal_change(self, file_path: str) -> bool:
        """
        Check if a file change is from Skrymir (not user).

        Checks:
        1. Recent fix marker
        2. Marker file exists
        """
        # Check recent fixes
        if file_path in self._recent_fixes:
            elapsed = time.time() - self._recent_fixes[file_path]
            if elapsed < self.FIX_COOLDOWN_SECONDS:
                logger.debug(f"File {file_path} changed within {elapsed:.1f}s of fix")
                return True
            else:
                # Expired, remove
                del self._recent_fixes[file_path]

        # Check marker file
        if self._marker_exists(file_path):
            return True

        return False

    def clear_internal_markers(self) -> None:
        """Clear all internal change markers (call after analysis cycle)."""
        self._recent_fixes.clear()

    def _create_marker_file(self, file_path: str) -> None:
        """Create a marker file indicating internal change."""
        marker_path = self._get_marker_path(file_path)
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.touch()
        except Exception as e:
            logger.warning(f"Could not create marker file: {e}")

    def _marker_exists(self, file_path: str) -> bool:
        """Check if marker file exists."""
        marker_path = self._get_marker_path(file_path)
        return marker_path.exists()

    def _get_marker_path(self, file_path: str) -> Path:
        """Get path to marker file."""
        # Store markers in .cerebro/markers/ relative to file
        file_path = Path(file_path)
        marker_dir = file_path.parent / ".cerebro" / "markers"
        return marker_dir / f"{file_path.name}.marker"

    def cleanup_expired_markers(self, project_path: str) -> None:
        """Remove expired marker files."""
        markers_dir = Path(project_path) / ".cerebro" / "markers"
        if not markers_dir.exists():
            return

        now = time.time()
        for marker in markers_dir.glob("*.marker"):
            elapsed = now - marker.stat().st_mtime
            if elapsed > self.FIX_COOLDOWN_SECONDS:
                try:
                    marker.unlink()
                except Exception:
                    pass
