"""The documents the assistant answers from, and a small BM25 index over them.

The corpus is this repository's ``docs/*.md`` plus ``README.md`` (override the
directory with ``DOCS_ASSISTANT_CORPUS``). Files are split into sections at
markdown headings. Retrieval is BM25 in pure Python: deterministic, no
embedding model to download, and good enough for keyword-heavy questions
about technical docs.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
_TOKEN = re.compile(r"[a-z0-9_]+")
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_STOP = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was", "what", "when",
    "where", "which", "who", "why", "with",
})


@dataclass(frozen=True)
class Section:
    path: str  # relative to the corpus root, e.g. "docs/REPLAY.md"
    heading: str
    text: str


def corpus_root() -> Path:
    return Path(os.environ.get("DOCS_ASSISTANT_CORPUS", str(REPO))).resolve()


def corpus_files(root: Path) -> list[Path]:
    files = sorted((root / "docs").glob("*.md"))
    readme = root / "README.md"
    return [readme, *files] if readme.exists() else files


def _sections(root: Path, path: Path) -> list[Section]:
    relative = path.relative_to(root).as_posix()
    out: list[Section] = []
    heading = path.stem
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _HEADING.match(line)
        if match and lines:
            out.append(Section(relative, heading, "\n".join(lines).strip()))
            lines = []
        if match:
            heading = match.group(2).strip()
        lines.append(line)
    if lines:
        out.append(Section(relative, heading, "\n".join(lines).strip()))
    return [s for s in out if len(s.text) > 40]


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class Index:
    """Okapi BM25 (k1 = 1.5, b = 0.75) over sections."""

    def __init__(self, sections: list[Section]) -> None:
        self.sections = sections
        self.docs = [Counter(tokenize(f"{s.heading} {s.text}")) for s in sections]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lengths) / max(len(self.docs), 1)
        df: Counter[str] = Counter()
        for doc in self.docs:
            df.update(doc.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 4) -> list[tuple[float, Section]]:
        terms = tokenize(query)
        scored = []
        for doc, length, section in zip(self.docs, self.lengths, self.sections, strict=True):
            score = 0.0
            for term in terms:
                tf = doc.get(term, 0)
                if tf:
                    norm = 1.5 * (0.25 + 0.75 * length / self.avg)
                    score += self.idf[term] * tf * 2.5 / (tf + norm)
            if score > 0:
                scored.append((score, section))
        scored.sort(key=lambda pair: (-pair[0], pair[1].path, pair[1].heading))
        return scored[:k]


@lru_cache(maxsize=4)
def index_for(root: Path) -> Index:
    return Index([s for path in corpus_files(root) for s in _sections(root, path)])
