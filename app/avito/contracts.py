"""Snapshot provenance and per-operation matching of owner-supplied official exports.

Matching is NOT a claim that a local file came from Avito or that an account has rights.
No live request uses the bundled secondary snapshot as its authorization to run.
"""

import json
from importlib.resources import files
from pathlib import Path

from app.avito.exceptions import ContractUnverified

IGNORED = {"description", "summary", "example", "examples", "title", "tags", "externalDocs"}


def resolve(document, value, depth=0):
    if depth > 60:
        raise ValueError("Recursive or excessive schema depth")
    if isinstance(value, list):
        return [resolve(document, v, depth + 1) for v in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        ref = value["$ref"]
        if not ref.startswith("#/"):
            raise ValueError("External schema references are not fetched")
        target = document
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return resolve(document, target, depth + 1)
    return {
        k: resolve(document, v, depth + 1)
        for k, v in value.items()
        if k not in IGNORED and not k.startswith("x-")
    }


def signature(document, path, method):
    container = document["paths"][path]
    operation = container[method]
    parameters = container.get("parameters", []) + operation.get("parameters", [])
    return resolve(
        document,
        {
            "parameters": parameters,
            "requestBody": operation.get("requestBody"),
            "responses": operation["responses"],
            "security": operation.get("security", document.get("security", [])),
        },
    )


def snapshot():
    return json.loads(files("app.avito").joinpath("contracts_snapshot.json").read_text("utf-8"))


class ContractRegistry:
    def __init__(self):
        self.operations = snapshot()["operations"]
        self.matched: set[str] = set()
        self.issues: dict[str, str] = {}

    @classmethod
    def from_official_exports(cls, paths: list[Path]):
        registry = cls()
        documents = []
        for path in paths:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            if "swagger" in document and isinstance(document["swagger"], str):
                document = json.loads(document["swagger"])
            if not str(document.get("openapi", "")).startswith("3.0."):
                raise ValueError("Expected an OpenAPI 3.0 export")
            documents.append(document)
        for name, expected in registry.operations.items():
            candidates = [
                d
                for d in documents
                if expected["method"] in d.get("paths", {}).get(expected["path"], {})
            ]
            if not candidates:
                registry.issues[name] = "Operation absent from supplied exports"
                continue
            try:
                if all(
                    signature(d, expected["path"], expected["method"]) == expected["signature"]
                    for d in candidates
                ):
                    registry.matched.add(name)
                else:
                    registry.issues[name] = "Contract drift; review before use"
            except (KeyError, TypeError, ValueError):
                registry.issues[name] = "Cannot resolve operation contract"
        return registry

    def require(self, operation: str):
        if operation not in self.matched:
            raise ContractUnverified(
                f"{operation}: official export not matched; live request blocked"
            )
        return self.operations[operation]
