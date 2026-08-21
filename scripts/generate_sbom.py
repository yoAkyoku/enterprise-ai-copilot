"""Generate a deterministic SPDX-lite inventory from installed Python packages."""

from __future__ import annotations

import argparse
import json
import sys
from importlib import metadata
from pathlib import Path


def build_document() -> dict[str, object]:
    packages = sorted(
        (
            (distribution.metadata.get("Name") or distribution.name).strip(),
            distribution.version,
        )
        for distribution in metadata.distributions()
        if distribution.metadata.get("Name") or distribution.name
    )
    unique_packages = list(dict.fromkeys(packages))
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "enterprise-ai-copilot-python-environment",
        "documentNamespace": "https://github.com/yoAkyoku/enterprise-ai-copilot/sbom",
        "creationInfo": {"createdBy": ["Tool: scripts/generate_sbom.py"]},
        "packages": [
            {
                "SPDXID": f"SPDXRef-Package-{index}",
                "name": name,
                "versionInfo": version,
                "downloadLocation": "NOASSERTION",
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
            }
            for index, (name, version) in enumerate(unique_packages, start=1)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_document(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote SBOM: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
