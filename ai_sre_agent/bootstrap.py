"""Create local-only lab configuration without overwriting existing secrets."""

import secrets
from pathlib import Path

import yaml
from dotenv import dotenv_values


def main():
    root = Path.cwd()
    if not (root / ".env").exists():
        (root / ".env").write_text(
            "POSTGRES_DB=sre_agent\nPOSTGRES_USER=sre\nPOSTGRES_PASSWORD="
            + secrets.token_urlsafe(32)
            + "\n",
            encoding="utf-8",
        )
    local = root / ".demo"
    local.mkdir(exist_ok=True)
    path = local / "lab.env"
    if not path.exists():
        names = [
            "API_TOKEN",
            "WEBHOOK_TOKEN",
            "APPROVER_TOKEN",
            "EXECUTOR_TOKEN",
            "DEMO_CONTROL_TOKEN",
        ]
        path.write_text(
            "".join(name + "=" + secrets.token_urlsafe(32) + "\n" for name in names),
            encoding="utf-8",
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass
    values = dotenv_values(path)
    configuration = yaml.safe_load(
        (root / "infra" / "alertmanager.yml").read_text(encoding="utf-8")
    )
    configuration["receivers"][0]["webhook_configs"][0]["http_config"] = {
        "authorization": {"credentials": values["WEBHOOK_TOKEN"]}
    }
    target = local / "alertmanager.yml"
    target.write_text(yaml.safe_dump(configuration), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    print("Local lab configuration prepared in .demo/; existing .env and tokens preserved.")


if __name__ == "__main__":
    main()
