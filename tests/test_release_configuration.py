from pathlib import Path

import yaml


def test_infrastructure_yaml_is_parseable_and_executor_is_opt_in():
    root = Path(__file__).parents[1]
    for path in (root / "infra").rglob("*.yml"):
        documents = list(yaml.safe_load_all(path.read_text()))
        assert documents and all(isinstance(item, dict) for item in documents)
    compose = yaml.safe_load((root / "infra/compose.yml").read_text())
    assert compose["services"]["executor"]["profiles"] == ["remediation"]
    assert "labdata" in compose["volumes"]
    assert (
        compose["services"]["api"]["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )
