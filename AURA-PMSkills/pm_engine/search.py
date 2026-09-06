"""Skill / command search and automatic skill loading.

Upstream: *"Skills are loaded automatically when relevant to the conversation —
no explicit invocation needed."*  This module reproduces that behaviour outside
Claude Code with a dependency-free, two-field BM25 index:

* **core** field — skill/command name + front-matter description (upstream
  deliberately packs descriptions with "Use when …" trigger phrases), weighted
  high;
* **body** field — workflow-step headings, the lead paragraph, and the plugin
  keywords, weighted low.

Adjacent query bigrams found in the core field ("north star", "pre mortem",
"feature request") add a phrase boost; exact name matches add more.
"""

from __future__ import annotations

import difflib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

from .registry import Command, Registry, Skill

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#\.\-]*")
_FENCE_RE = re.compile(r"```.*?```", re.S)

_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "he", "in", "is", "it", "its",
    "of", "on", "or", "that", "the", "to", "was", "were", "will", "with", "we", "our", "you", "your", "i",
    "me", "my", "this", "these", "those", "into", "use", "when", "using", "help", "please", "can", "how",
    "what", "need", "want", "should", "do", "does", "about", "them", "they", "their", "us", "up", "so",
    "here", "there", "some", "any", "all", "if", "then", "than", "also", "more", "most", "very", "just",
    "like", "get", "make", "give", "run", "which", "who", "whom", "would", "could", "have", "had", "not",
}

# ordered suffix rules → (suffix, replacement, min_stem_len)
_RULES = (
    ("ization", "ize", 3), ("isation", "ize", 3), ("izations", "ize", 3),
    ("iest", "y", 3), ("ier", "y", 3), ("ies", "y", 3), ("ying", "y", 2),
    ("ations", "ate", 3), ("ation", "ate", 3), ("ative", "ate", 3),
    ("ments", "", 4), ("ment", "", 4), ("ities", "", 4), ("ity", "", 4),
    ("ings", "", 3), ("ing", "", 3), ("ers", "", 4), ("er", "", 4),
    ("ness", "", 4), ("ful", "", 4), ("less", "", 4), ("ous", "", 4),
    ("es", "", 3), ("s", "", 3), ("ed", "", 3), ("ly", "", 4),
)


def _stem(tok: str) -> str:
    if len(tok) <= 3:
        return tok
    for suf, rep, mn in _RULES:
        if tok.endswith(suf) and len(tok) - len(suf) >= mn:
            tok = tok[: -len(suf)] + rep
            break
    # normalise trailing e / y so write/writing, risky/risk, strategy/strategic align
    if len(tok) > 4 and tok.endswith("e"):
        tok = tok[:-1]
    if len(tok) > 4 and tok.endswith("y"):
        tok = tok[:-1]
    if len(tok) > 4 and tok.endswith("ic"):
        tok = tok[:-2]
    return tok


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for t in _TOKEN_RE.findall(text.lower()):
        t = t.strip(".-")
        if not t or t in _STOP:
            continue
        out.append(_stem(t))
        if "-" in t:  # pre-mortem → pre, mortem (in addition to the joined form)
            out.extend(_stem(p) for p in t.split("-") if p and p not in _STOP)
    return out


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub(" ", text)


def _bigrams(tokens: Sequence[str]) -> set[tuple[str, str]]:
    return {(tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1)}


@dataclass
class Hit:
    kind: str  # "skill" | "command"
    name: str
    plugin: str
    score: float
    description: str
    item: object

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}:{self.name}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "plugin": self.plugin,
            "qualified_name": self.qualified_name,
            "score": round(self.score, 4),
            "description": self.description,
        }


class _BM25:
    def __init__(self, k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs: list[Counter] = []
        self.doc_len: list[int] = []
        self.df: Counter = Counter()
        self.avgdl = 1.0

    def add(self, tokens: Sequence[str]) -> int:
        c = Counter(tokens)
        self.docs.append(c)
        self.doc_len.append(max(len(tokens), 1))
        for t in c:
            self.df[t] += 1
        self.avgdl = sum(self.doc_len) / len(self.doc_len)
        return len(self.docs) - 1

    def idf(self, term: str) -> float:
        n = len(self.docs)
        df = self.df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def score(self, idx: int, query: Sequence[str]) -> float:
        doc = self.docs[idx]
        dl = self.doc_len[idx]
        s = 0.0
        for q in query:
            tf = doc.get(q)
            if not tf:
                continue
            denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            s += self.idf(q) * tf * (self.k1 + 1) / denom
        return s


class SkillIndex:
    """Two-field BM25 index over all skills and commands in a :class:`Registry`."""

    CORE_WEIGHT = 1.0
    BODY_WEIGHT = 0.35
    PHRASE_BOOST = 1.5
    NAME_BOOST = 3.0

    def __init__(self, registry: Registry):
        self.registry = registry
        self._core = _BM25()
        self._body = _BM25()
        self._items: list[tuple[str, object]] = []
        self._core_bigrams: list[set[tuple[str, str]]] = []
        self._name_tokens: list[set[str]] = []
        self._build()

    # -- building --------------------------------------------------------

    @staticmethod
    def _core_text(item: Skill | Command) -> str:
        return f"{item.name.replace('-', ' ')} {item.name.replace('-', ' ')} {item.description}"

    @staticmethod
    def _body_text(item: Skill | Command, plugin_keywords: Iterable[str]) -> str:
        body = _strip_fences(item.body)
        heads = re.findall(r"^#{1,4}\s+(.+)$", body, flags=re.M)
        if isinstance(item, Command):
            heads = [h for h in heads if re.match(r"(Step|\d+\.|.+ Mode$)", h)]
            extra = item.argument_hint
        else:
            extra = ""
        lead = " ".join(body.split()[:100])
        return " ".join([" ".join(heads), lead, extra, " ".join(plugin_keywords)])

    def _add(self, kind: str, item: Skill | Command, plugin_keywords: Iterable[str]) -> None:
        self._items.append((kind, item))
        core_tokens = tokenize(self._core_text(item))
        self._core.add(core_tokens)
        self._body.add(tokenize(self._body_text(item, plugin_keywords)))
        self._core_bigrams.append(_bigrams(core_tokens))
        self._name_tokens.append(set(tokenize(item.name)))

    def _build(self) -> None:
        for plugin in self.registry.plugins.values():
            for s in plugin.skills.values():
                self._add("skill", s, plugin.keywords)
            for c in plugin.commands.values():
                self._add("command", c, plugin.keywords)

    # -- querying --------------------------------------------------------

    def _score(self, idx: int, q: list[str], qset: set[str], qbi: set[tuple[str, str]]) -> float:
        score = self.CORE_WEIGHT * self._core.score(idx, q) + self.BODY_WEIGHT * self._body.score(idx, q)
        if score <= 0:
            return 0.0
        phrase_hits = len(qbi & self._core_bigrams[idx])
        score += self.PHRASE_BOOST * phrase_hits
        name_toks = self._name_tokens[idx]
        if name_toks and name_toks <= qset:
            score += self.NAME_BOOST
        elif name_toks & qset:
            score += 0.5 * len(name_toks & qset)
        return score

    def search(self, query: str, *, kind: str | None = None, limit: int = 10, plugin: str | None = None) -> list[Hit]:
        q = tokenize(query)
        if not q:
            return []
        qset, qbi = set(q), _bigrams(q)
        hits: list[Hit] = []
        for idx, (k, item) in enumerate(self._items):
            if kind and k != kind:
                continue
            if plugin and item.plugin != plugin:  # type: ignore[attr-defined]
                continue
            score = self._score(idx, q, qset, qbi)
            if score <= 0:
                continue
            hits.append(Hit(kind=k, name=item.name, plugin=item.plugin, score=score, description=item.description, item=item))  # type: ignore[attr-defined]
        hits.sort(key=lambda h: (-h.score, h.name))
        return hits[:limit]

    def auto_load(self, text: str, *, limit: int = 3, min_score: float = 4.0, relative: float = 0.6) -> list[Skill]:
        """Skills that should be loaded automatically for *text*.

        A skill qualifies when its score is above ``min_score`` **and** at least
        ``relative`` × the top score — the one or two obviously-relevant skills,
        not a long tail.
        """
        hits = self.search(text, kind="skill", limit=max(limit * 3, 10))
        if not hits:
            return []
        top = hits[0].score
        chosen: list[Skill] = []
        for h in hits:
            if h.score >= min_score and h.score >= relative * top:
                chosen.append(h.item)  # type: ignore[arg-type]
            if len(chosen) >= limit:
                break
        return chosen

    def suggest_command(self, text: str, min_score: float = 6.0) -> Command | None:
        hits = self.search(text, kind="command", limit=1)
        return hits[0].item if hits and hits[0].score >= min_score else None  # type: ignore[return-value]

    def did_you_mean(self, ref: str, limit: int = 3) -> list[Hit]:
        """Fuzzy-match a mistyped ``/name`` against every skill and command name."""
        ref = ref.strip().lstrip("/").lower()
        if ":" in ref:
            ref = ref.split(":", 1)[1]
        names: dict[str, tuple[str, object]] = {}
        for k, item in self._items:
            names.setdefault(item.name, (k, item))  # type: ignore[attr-defined]
        squashed = {n.replace("-", ""): n for n in names}
        candidates = difflib.get_close_matches(ref, list(names) + list(squashed), n=limit * 2, cutoff=0.6)
        out: list[Hit] = []
        for c in candidates:
            name = squashed.get(c, c)
            k, item = names[name]
            ratio = difflib.SequenceMatcher(None, ref.replace("-", ""), name.replace("-", "")).ratio()
            if all(h.name != name for h in out):
                out.append(Hit(kind=k, name=name, plugin=item.plugin, score=ratio, description=item.description, item=item))  # type: ignore[attr-defined]
        out.sort(key=lambda h: -h.score)
        return out[:limit]

    def explain(self, query: str, ref: str) -> dict:
        q = tokenize(query)
        for idx, (k, item) in enumerate(self._items):
            if item.name == ref or f"{item.plugin}:{item.name}" == ref:  # type: ignore[attr-defined]
                core, body = self._core.docs[idx], self._body.docs[idx]
                return {
                    "ref": ref,
                    "kind": k,
                    "core_terms": {t: core[t] for t in q if t in core},
                    "body_terms": {t: body[t] for t in q if t in body},
                    "phrases": sorted(_bigrams(q) & self._core_bigrams[idx]),
                    "score": self._score(idx, q, set(q), _bigrams(q)),
                }
        return {"ref": ref, "error": "not found"}


def related_skills(registry: Registry, skill: Skill, limit: int = 5) -> list[Skill]:
    idx = SkillIndex(registry)
    hits = idx.search(skill.description + " " + skill.name.replace("-", " "), kind="skill", limit=limit + 1)
    out = [h.item for h in hits if h.item is not skill]
    out.sort(key=lambda s: (s.plugin != skill.plugin))  # type: ignore[attr-defined]
    return out[:limit]  # type: ignore[return-value]


def group_by_plugin(hits: Iterable[Hit]) -> dict[str, list[Hit]]:
    g: dict[str, list[Hit]] = defaultdict(list)
    for h in hits:
        g[h.plugin].append(h)
    return dict(g)
