import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts.check_epig_after_sales_contract import (
    DEFAULT_BUNDLE,
    DEFAULT_PRODUCT_DOC,
    ContractError,
    resolve_pointer,
    validate_bundle,
)


def copy_bundle(tmp_path: Path) -> Path:
    target = tmp_path / "v1"
    shutil.copytree(DEFAULT_BUNDLE, target)
    return target


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def test_canonical_contract_and_fixtures_are_valid() -> None:
    validate_bundle()


def test_checker_rejects_a_valid_fixture_missing_a_required_field(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    document = load(bundle / "openapi.json")
    manifest_path = bundle / "fixtures" / "manifest.json"
    manifest = load(manifest_path)
    entry = next(
        item
        for item in manifest["fixtures"]
        if item["kind"] == "request" and item["expected_valid"]
    )
    schema = resolve_pointer(document, entry["schema"])
    required_field = schema["required"][0]
    fixture_path = bundle / "fixtures" / entry["path"]
    fixture = load(fixture_path)
    del fixture[required_field]
    save(fixture_path, fixture)

    with pytest.raises(ContractError, match="expected valid fixture"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC)


def test_checker_rejects_an_unreferenced_fixture(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    (bundle / "fixtures" / "orphan.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ContractError, match="not referenced"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC)


def test_checker_rejects_an_unknown_requirement_id(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    openapi_path = bundle / "openapi.json"
    document = load(openapi_path)
    operation = next(
        operation
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if isinstance(operation, dict) and "operationId" in operation
    )
    operation["x-requirement-ids"] = ["AS-REQ-UNKNOWN-999"]
    save(openapi_path, document)

    with pytest.raises(ContractError, match="unknown requirement IDs"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC)


def test_checker_rejects_a_version_mismatch(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    manifest_path = bundle / "fixtures" / "manifest.json"
    manifest = load(manifest_path)
    manifest["contract_version"] = "1.0.1"
    save(manifest_path, manifest)

    with pytest.raises(ContractError, match="version 1.0.0"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC)


def test_checker_rejects_an_invented_consumer_cookie_name(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    openapi_path = bundle / "openapi.json"
    document = load(openapi_path)
    document["components"]["securitySchemes"]["ConsumerOrderOwnershipCookie"]["name"] = (
        "e_pig_order_session"
    )
    save(openapi_path, document)

    with pytest.raises(ContractError, match="epig_cart_"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC)


def test_checker_rejects_a_manifest_schema_pointer_mismatch(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    manifest_path = bundle / "fixtures" / "manifest.json"
    manifest = load(manifest_path)
    entry = next(item for item in manifest["fixtures"] if item["kind"] == "response")
    entry["schema"] = "#/components/schemas/OpaqueId"
    save(manifest_path, manifest)

    with pytest.raises(ContractError, match="schema pointer"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC)


def test_checker_rejects_an_error_response_without_a_version_header(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    openapi_path = bundle / "openapi.json"
    document = load(openapi_path)
    headers = document["components"]["responses"]["InternalError"]["headers"]
    header_name = next(name for name in headers if name.lower() == "x-after-sales-contract-version")
    del headers[header_name]
    save(openapi_path, document)

    with pytest.raises(ContractError, match="response lacks wire-major header"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC)


def test_checker_rejects_consumer_response_additional_properties(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    openapi_path = bundle / "openapi.json"
    document = load(openapi_path)
    document["components"]["schemas"]["ConsumerTicket"]["additionalProperties"] = True
    save(openapi_path, document)

    with pytest.raises(ContractError, match="producer allowlist"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC)


def test_checker_rejects_a_binary_digest_mismatch(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    manifest_path = bundle / "fixtures" / "manifest.json"
    manifest = load(manifest_path)
    entry = next(item for item in manifest["fixtures"] if item.get("representation") == "binary")
    entry["sha256"] = "0" * 64
    save(manifest_path, manifest)

    with pytest.raises(ContractError, match="SHA-256 mismatch"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC)


def test_checker_rejects_stale_contract_checksums(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    (bundle / "SHA256SUMS").write_text("stale\n", encoding="utf-8")

    with pytest.raises(ContractError, match="SHA256SUMS is stale"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC)


def test_checker_rejects_an_optional_response_version_header(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    openapi_path = bundle / "openapi.json"
    document = load(openapi_path)
    headers = document["components"]["responses"]["InternalError"]["headers"]
    header_name = next(name for name in headers if name.lower() == "x-after-sales-contract-version")
    header = headers[header_name]
    if "$ref" in header:
        pointer = header["$ref"].split("/")[-1]
        document["components"]["headers"][pointer]["required"] = False
    else:
        header["required"] = False
    save(openapi_path, document)

    with pytest.raises(ContractError, match="header must be required"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC)


def test_checker_rejects_a_valid_binary_over_five_mib(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    manifest_path = bundle / "fixtures" / "manifest.json"
    manifest = load(manifest_path)
    entry = next(item for item in manifest["fixtures"] if item.get("representation") == "binary")
    binary_path = bundle / "fixtures" / entry["path"]
    data = binary_path.read_bytes() + (b"\0" * (5 * 1024 * 1024))
    binary_path.write_bytes(data)
    entry["size_bytes"] = len(data)
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    save(manifest_path, manifest)

    with pytest.raises(ContractError, match="exceeds 5 MiB"):
        validate_bundle(bundle, DEFAULT_PRODUCT_DOC, check_checksums=False)
