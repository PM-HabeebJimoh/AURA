"""Re-sync the vendored marketplace from upstream ``phuryn/pm-skills``.

Only the content the engine needs is vendored (plugins, manifests, validator,
tests, docs). Large binary assets (the 5 MB install GIF) and upstream CI
workflows are skipped. The commit that was vendored is recorded in
``pm-skills/UPSTREAM`` so drift is auditable.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_URL = "https://github.com/phuryn/pm-skills.git"

KEEP_TOP_LEVEL = (
    ".claude-plugin",
    "tests",
    "validate_plugins.py",
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    ".gitattributes",
)
SKIP_IMAGES = ("pm-skills-install.gif",)


@dataclass
class UpdateResult:
    commit: str
    version: str
    plugins: list[str]
    destination: Path

    def to_dict(self) -> dict:
        return {"commit": self.commit, "version": self.version, "plugins": self.plugins, "destination": str(self.destination)}


def current_upstream(marketplace_dir: Path) -> dict:
    p = marketplace_dir / "UPSTREAM"
    if not p.is_file():
        return {}
    text = p.read_text(encoding="utf-8")
    m = re.search(r"@ ([0-9a-f]{7,40}) \(v?([\d.]+)\)", text)
    return {"commit": m.group(1), "version": m.group(2)} if m else {"raw": text}


def sync_from_checkout(src: Path, dest: Path, commit: str) -> UpdateResult:
    dest.mkdir(parents=True, exist_ok=True)
    # remove previously vendored plugin dirs so deletions upstream propagate
    for old in dest.glob("pm-*"):
        if old.is_dir():
            shutil.rmtree(old)
    plugins: list[str] = []
    for p in sorted(src.glob("pm-*")):
        if p.is_dir() and (p / ".claude-plugin" / "plugin.json").is_file():
            shutil.copytree(p, dest / p.name, ignore=shutil.ignore_patterns("__pycache__"))
            plugins.append(p.name)
    for name in KEEP_TOP_LEVEL:
        s = src / name
        if s.is_dir():
            if (dest / name).exists():
                shutil.rmtree(dest / name)
            shutil.copytree(s, dest / name, ignore=shutil.ignore_patterns("__pycache__"))
        elif s.is_file():
            shutil.copyfile(s, dest / name)
    images = src / ".docs" / "images"
    if images.is_dir():
        (dest / ".docs" / "images").mkdir(parents=True, exist_ok=True)
        for img in images.iterdir():
            if img.name not in SKIP_IMAGES:
                shutil.copyfile(img, dest / ".docs" / "images" / img.name)

    version = ""
    mp = dest / ".claude-plugin" / "marketplace.json"
    if mp.is_file():
        m = re.search(r'"version"\s*:\s*"([^"]+)"', mp.read_text(encoding="utf-8"))
        version = m.group(1) if m else ""
    (dest / "UPSTREAM").write_text(
        f"# Upstream: {UPSTREAM_URL.removesuffix('.git')} @ {commit} (v{version})\n"
        "# Vendored into AURA-PMSkills. Do not edit by hand — run: pm-engine update-skills\n",
        encoding="utf-8",
    )
    return UpdateResult(commit=commit, version=version, plugins=plugins, destination=dest)


def update(dest: Path, *, ref: str = "main", url: str = UPSTREAM_URL) -> UpdateResult:
    with tempfile.TemporaryDirectory(prefix="pm-skills-") as tmp:
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", ref, url, tmp], check=True)
        commit = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        return sync_from_checkout(Path(tmp), dest, commit)
