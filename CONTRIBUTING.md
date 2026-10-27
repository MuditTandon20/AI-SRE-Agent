# Contributing

This project is in early development. See [the plan](PROJECT_PLAN.md) for scope, [the full-stack guide](docs/full-stack-quickstart.md) for setup, and [the architecture](docs/architecture.md) for connector contracts and limits.

Open an issue describing the problem and expected behavior before proposing a large feature. Small bug fixes, documentation improvements, and reproducible incident fixtures are welcome. Keep each pull request focused, explain user-visible changes and include meaningful validation. Preserve compatibility or document breaking changes.

Run `python -m ruff check .`, `python -m ruff format --check .`, and `python -m pytest -q`. Run PostgreSQL integration tests when changing persistence; CI uses a disposable database. Ordinary tests need no paid model account. Never include private logs, credentials or production incident data in fixtures, issues or pull requests.

The maintainer reviews contributions before merging. Changes to public contracts, dependencies and external data handling should include rationale and documentation. Contributions are made under the project's existing MIT license.
