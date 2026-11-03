"""Validate project-owned Markdown and development skills without network access.

This checks packaging and local links, not rendering, application behavior,
external URLs, or the truth of design and verification claims.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT_FILES = (
    "AGENTS.md",
    "README.md",
    "CONTRIBUTING.md",
    ".dtc/target-profile.md",
    "docs/README.md",
    "docs/project-plan.md",
)
MANAGED_DIRS = (
    "docs/guides",
    "docs/learning",
    "docs/architecture",
    "docs/records",
    ".agents/skills",
)


def prose(text: str) -> str:
    """Remove fenced code so example paths do not become real link obligations."""
    return re.sub(r"(?m)^(`{3,}|~{3,})[^\n]*\n[\s\S]*?^\1\s*$", "", text)


def anchors(text: str) -> set[str]:
    """Recognize ordinary GitHub headings and explicit HTML id anchors."""
    result = set(re.findall(r'\bid=["\']([^"\']+)["\']', text))
    seen: dict[str, int] = {}
    for heading in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*#*\s*$", prose(text)):
        heading = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", heading)
        heading = re.sub(r"<[^>]+>", "", heading).lower().strip()
        slug = "".join(
            char for char in heading if char in "-_ " or unicodedata.category(char)[0] in "LNM"
        ).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(f"{slug}-{count}" if count else slug)
    return result


def destinations(text: str) -> list[str]:
    """Read inline/ref-style Markdown links and HTML href/src attributes."""
    text = prose(text)
    found = re.findall(r"\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)", text)
    found += re.findall(r"(?m)^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)", text)
    found += re.findall(r'\b(?:src|href)=["\']([^"\']+)["\']', text)
    return [value.strip("<>") for value in found]


def check(root: Path) -> tuple[list[str], int, int]:
    """Return structural errors and Markdown/skill counts."""
    root = root.resolve()
    errors: list[str] = []
    files = {root / path for path in ROOT_FILES}
    for folder in MANAGED_DIRS:
        files.update((root / folder).rglob("*.md"))
    for path in sorted(files):
        label = str(path.relative_to(root))
        if not path.is_file():
            errors.append(f"{label}: required Markdown file missing")
            continue
        if path.is_symlink():
            errors.append(f"{label}: documentation must be a repository file")
            continue
        for link in destinations(path.read_text(encoding="utf-8")):
            try:
                parts = urlsplit(link)
            except ValueError:
                errors.append(f"{label}: invalid link {link}")
                continue
            if parts.scheme or parts.netloc:
                continue
            target = (path.parent / unquote(parts.path)).resolve() if parts.path else path
            if not target.is_relative_to(root):
                errors.append(f"{label}: local link escapes repository: {link}")
            elif not target.exists():
                errors.append(f"{label}: missing local target: {link}")
            elif parts.fragment and target.is_file() and target.suffix.lower() == ".md":
                if unquote(parts.fragment) not in anchors(target.read_text(encoding="utf-8")):
                    errors.append(f"{label}: missing Markdown anchor: {link}")

    skill_root = root / ".agents/skills"
    try:
        manifest = json.loads((skill_root / "sources.json").read_text(encoding="utf-8"))
        entries = manifest["skills"]
        if not isinstance(entries, list) or not entries:
            raise ValueError("skills must be a nonempty list")
        names = [entry["name"] for entry in entries]
        if len(names) != len(set(names)):
            errors.append("sources.json: duplicate skill names")
        actual = {path.parent.name for path in skill_root.glob("*/SKILL.md")}
        if actual != set(names):
            errors.append(f"sources.json: directory mismatch: {sorted(actual ^ set(names))}")
        if not (skill_root / manifest["upstream"]["license"]).is_file():
            errors.append("sources.json: source license missing")
        for group in (
            manifest["upstream"],
            manifest["documentation_rules"],
            manifest["local_skill_library"],
        ):
            if not re.fullmatch(r"[0-9a-f]{40}", group.get("commit", "")):
                errors.append("sources.json: invalid source commit")
            for digest in group.get("files", {}).values():
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    errors.append("sources.json: invalid source content digest")
        for reference in manifest["upstream"].get("reference_files", []):
            if not re.fullmatch(r"[0-9a-f]{64}", reference.get("sha256", "")):
                errors.append("sources.json: invalid reference content digest")
        agent_rules = (root / "AGENTS.md").read_text(encoding="utf-8")
        for entry in entries:
            name = entry["name"]
            if "source_path" in entry and not re.fullmatch(
                r"[0-9a-f]{64}", entry.get("source_sha256", "")
            ):
                errors.append(f"sources.json: invalid skill source digest for {name}")
            if not re.fullmatch(r"dtc-[a-z0-9-]+", name):
                errors.append(f"sources.json: invalid skill name {name}")
                continue
            if entry["path"] != f"{name}/SKILL.md":
                errors.append(f"sources.json: wrong entry path for {name}")
            if f".agents/skills/{name}/SKILL.md" not in agent_rules:
                errors.append(f"AGENTS.md: missing skill route {name}")
            skill = skill_root / name / "SKILL.md"
            if not skill.is_file():
                errors.append(f"{name}: SKILL.md missing")
                continue
            text = skill.read_text(encoding="utf-8")
            header = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
            if header is None:
                errors.append(f"{name}: YAML frontmatter missing")
            else:
                fields = dict(re.findall(r"(?m)^(name|description):\s*(.+)$", header[1]))
                if fields.get("name") != name or not fields.get("description"):
                    errors.append(f"{name}: frontmatter name/description invalid")
            metadata = skill.parent / "agents/openai.yaml"
            try:
                lines = metadata.read_text(encoding="utf-8").splitlines()
                if not lines or lines[0] != "interface:":
                    raise ValueError("expected interface mapping")
                values = {}
                for line in lines[1:]:
                    key, value = line.strip().split(":", 1)
                    values[key] = json.loads(value)
                if not values.get("display_name"):
                    raise ValueError("missing display_name")
                if not 25 <= len(values.get("short_description", "")) <= 64:
                    raise ValueError("short_description length must be 25..64")
                if f"${name}" not in values.get("default_prompt", ""):
                    raise ValueError("default_prompt must mention skill")
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"{name}: invalid agents/openai.yaml: {exc}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"sources.json: cannot validate manifest: {exc}")
        entries = []
    return errors, len(files), len(entries)


def main() -> int:
    """Run local checks and print a bounded summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors, count, skills = check(args.root)
    if errors:
        print("Project documentation check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"PASS: {count} managed Markdown files; {skills} development skills.")
    print("Offline structural checks only; no external URLs or application behavior tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
