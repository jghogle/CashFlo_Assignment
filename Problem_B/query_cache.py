"""
query_cache.py — Caches previously asked questions and their SQL.
Uses character-level trigram similarity so no ML dependencies are needed.
"""

from __future__ import annotations
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def _trigrams(text: str) -> set:
    t = re.sub(r"\s+", " ", text.lower().strip())
    return {t[i:i+3] for i in range(len(t) - 2)} if len(t) >= 3 else {t}


def similarity(a: str, b: str) -> float:
    """Jaccard similarity on trigrams — returns 0.0 to 1.0."""
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class QueryCache:
    def __init__(self, cache_path: str | Path, threshold: float = 0.75):
        self.path = Path(cache_path)
        self.threshold = threshold
        self.entries: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.entries = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2), encoding="utf-8")

    def lookup(self, question: str) -> Optional[Dict[str, Any]]:
        """Return best matching cached entry if similarity >= threshold."""
        best_score = 0.0
        best_entry = None
        for entry in self.entries:
            score = similarity(question, entry["question"])
            if score > best_score:
                best_score = score
                best_entry = entry
        if best_score >= self.threshold and best_entry:
            return {**best_entry, "cache_hit": True, "similarity": round(best_score, 3)}
        return None

    def store(self, question: str, sql: str, result_preview: Any,
              explanation: str, assumptions: str = "") -> None:
        """Add or update an entry in the cache."""
        # Update if very similar entry exists (score > 0.9)
        for entry in self.entries:
            if similarity(question, entry["question"]) > 0.9:
                entry.update({
                    "question": question,
                    "sql": sql,
                    "result_preview": result_preview,
                    "explanation": explanation,
                    "assumptions": assumptions,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                self._save()
                return

        self.entries.append({
            "question": question,
            "sql": sql,
            "result_preview": result_preview,
            "explanation": explanation,
            "assumptions": assumptions,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hit_count": 0,
        })
        self._save()

    def increment_hit(self, question: str) -> None:
        for entry in self.entries:
            if entry["question"] == question:
                entry["hit_count"] = entry.get("hit_count", 0) + 1
                self._save()
                return

    def all_entries(self) -> List[Dict]:
        return list(reversed(self.entries))

    def clear(self) -> None:
        self.entries = []
        self._save()
