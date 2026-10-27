"""Inspect built artifacts for resources and accidental private-file inclusion."""

from pathlib import Path
from tarfile import open as tar_open
from zipfile import ZipFile


def main():
    wheel = next(Path("dist").glob("*.whl"))
    source = next(Path("dist").glob("*.tar.gz"))
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "ai_sre_agent/migrations/002_investigation.sql" in names
        assert "ai_sre_agent/runbooks/database.md" in names
        assert "ai_sre_agent/launch.py" in names
        assert any(name.endswith("entry_points.txt") for name in names)
    with tar_open(source) as archive:
        source_names = [name.partition("/")[2] for name in archive.getnames()]
        for name in (
            "tests/conftest.py",
            "infra/compose.yml",
            "constraints.txt",
            "main.py",
            ".env.example",
        ):
            assert name in source_names, name
        names += source_names
    assert not any(
        name == ".env" or name.startswith((".demo/", ".aws/", ".venv/")) for name in names
    )
    print(
        "Package verification passed: wheel resources, console entrypoints, source lab/test files and private-file exclusions."
    )


if __name__ == "__main__":
    main()
