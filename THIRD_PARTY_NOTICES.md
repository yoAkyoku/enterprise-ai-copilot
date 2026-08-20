# Third-party notices

The runtime currently depends on the following open-source packages in the
published Python distribution:

- FastAPI — MIT License
- Uvicorn — BSD 3-Clause License
- PyYAML — MIT License
- tzdata — IANA time-zone data distribution terms

Development-only tooling also uses Ruff, `httpx2` for the FastAPI test client,
and pip-audit for dependency scanning. Their transitive notices must be
included in a final community-release inventory.

Their transitive dependencies are resolved by the package installer. Release
automation must regenerate a complete dependency and license inventory before a
community release. The synthetic demo itself contains no proprietary ERP SDK,
model credential or private connector.
