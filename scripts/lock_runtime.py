"""Record installed runtime dependency closure after successful environment tests."""

from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def main():
    queue = [
        Requirement(line)
        for line in Path("requirements.txt").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    seen = {}
    processed = set()
    while queue:
        requirement = queue.pop()
        name = canonicalize_name(requirement.name)
        key = (name, tuple(sorted(requirement.extras)))
        if key in processed:
            continue
        processed.add(key)
        installed = distribution(name)
        seen[name] = installed.version
        extras = requirement.extras | {""}
        for dependency in installed.requires or []:
            parsed = Requirement(dependency)
            if parsed.marker is None or any(
                parsed.marker.evaluate({"extra": extra}) for extra in extras
            ):
                queue.append(parsed)
    lines = [
        "# Installed runtime dependency closure. Regenerate only after testing an update.",
        "# Versions are pinned; platform/Python compatibility is verified by CI and image builds.",
    ]
    lines.extend(f"{name}=={version}" for name, version in sorted(seen.items()))
    Path("constraints.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Recorded {len(seen)} runtime constraints")


if __name__ == "__main__":
    main()
