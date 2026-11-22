"""Session JSON persistence."""

import json
import tempfile
from pathlib import Path


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def save(self, session):
        path = self.path(session["id"])
        # History and task context share one snapshot. Publish it only after
        # the complete JSON is written.
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(session, handle, indent=2)
            temporary_path.replace(path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
