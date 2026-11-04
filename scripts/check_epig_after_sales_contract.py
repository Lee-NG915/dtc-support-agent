#!/usr/bin/env python3
"""Validate the canonical E-pig after-sales contract and shared fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate as validate_openapi

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = ROOT / "contracts" / "epig-after-sales" / "v1"
DEFAULT_PRODUCT_DOC = (
    ROOT / "docs" / "architecture" / "modules" / "after-sales" / "product-blueprint.md"
)
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
REQUIREMENT_ID = re.compile(r"AS-REQ-[A-Z]+-\d{3}")
ACCEPTANCE_ID = re.compile(r"AS-ACC-\d{3}")
CONTRACT_HEADER = "x-after-sales-contract-version"
WIRE_MAJOR = "1"
REQUIRED_OPERATION_IDS = {
    "uploadAttachment",
    "getAttachmentContent",
    "getServiceAttachmentContent",
    "createAfterSalesRequest",
    "getAfterSalesRequest",
    "getConsumerTicket",
    "getServiceOrder",
    "getServiceOrderShipment",
    "listServiceRequests",
    "getServiceRequest",
    "createServiceTicket",
    "getServiceTicket",
    "getServiceTicketByIdempotencyKey",
}
CONSUMER_ALLOWLIST_SCHEMAS = {
    "Attachment",
    "RequestProgress",
    "ConsumerAfterSalesRequest",
    "ConsumerTicketReceipt",
    "ConsumerTicket",
    "RecipientSummary",
}
ERROR_STATUSES = {
    "VALIDATION_ERROR": {400},
    "UNSUPPORTED_CONTRACT_VERSION": {400},
    "INVALID_CURSOR": {400},
    "UNAUTHORIZED": {401},
    "FORBIDDEN_MARKET": {403},
    "NOT_FOUND": {404},
    "IDEMPOTENCY_CONFLICT": {409},
    "REQUEST_STATE_CONFLICT": {409},
    "ORDER_VERSION_CONFLICT": {409},
    "ATTACHMENT_EXPIRED": {410},
    "ATTACHMENT_TOO_LARGE": {413},
    "UNSUPPORTED_MEDIA_TYPE": {415},
    "RATE_LIMITED": {429},
    "INTERNAL_ERROR": {500},
    "UPSTREAM_UNAVAILABLE": {503},
}


class ContractError(ValueError):
    """Raised when the contract bundle or its conformance fixtures drift."""


def load_json(path: Path) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ContractError(f"{path}: duplicate JSON object keys")
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{path}: cannot load JSON: {exc}") from exc


def resolve_pointer(document: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or not pointer.startswith("#/"):
        raise ContractError(f"only local JSON pointers are supported: {pointer}")
    value = document
    for raw_part in pointer[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            raise ContractError(f"unresolved JSON pointer: {pointer}")
        value = value[part]
    return value


def resolve_reference(document: Any, value: Any) -> Any:
    seen: set[str] = set()
    while isinstance(value, dict) and "$ref" in value:
        reference = value["$ref"]
        if reference in seen:
            raise ContractError(f"cyclic reference: {reference}")
        seen.add(reference)
        value = resolve_pointer(document, reference)
    return value


def operations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path, path_item in document.get("paths", {}).items():
        if not path.startswith("/api/{market}/after-sales/"):
            raise ContractError(f"unexpected after-sales path: {path}")
        for method in HTTP_METHODS & path_item.keys():
            operation = path_item[method]
            operation_id = operation.get("operationId")
            if not operation_id:
                raise ContractError(f"{method.upper()} {path}: missing operationId")
            if operation_id in result:
                raise ContractError(f"duplicate operationId: {operation_id}")
            result[operation_id] = {
                **operation,
                "x-path": path,
                "x-method": method,
                "x-path-item": path_item,
            }
    missing = REQUIRED_OPERATION_IDS - set(result)
    if missing:
        raise ContractError(f"required operations missing: {sorted(missing)}")
    return result


def parameter_map(document: dict[str, Any], operation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    parameters = [
        *resolve_reference(document, operation["x-path-item"]).get("parameters", []),
        *operation.get("parameters", []),
    ]
    result: dict[str, dict[str, Any]] = {}
    for parameter in parameters:
        parameter = resolve_reference(document, parameter)
        key = (parameter.get("name", "")).lower()
        result[f"{parameter.get('in')}:{key}"] = parameter
    return result


def is_wire_major_schema(schema: dict[str, Any]) -> bool:
    return schema.get("const") == WIRE_MAJOR or schema.get("enum") == [WIRE_MAJOR]


def assert_contract_headers(document: dict[str, Any], operation: dict[str, Any]) -> None:
    operation_id = operation["operationId"]
    parameter = parameter_map(document, operation).get(f"header:{CONTRACT_HEADER}")
    if parameter is None or parameter.get("required") is not True:
        raise ContractError(f"{operation_id}: missing required wire-major request header")
    schema = resolve_reference(document, parameter.get("schema", {}))
    if not is_wire_major_schema(schema):
        raise ContractError(f"{operation_id}: request header must allow only wire major 1")

    for status, response_value in operation.get("responses", {}).items():
        response = resolve_reference(document, response_value)
        headers = {
            name.lower(): resolve_reference(document, header)
            for name, header in response.get("headers", {}).items()
        }
        header = headers.get(CONTRACT_HEADER)
        if header is None:
            raise ContractError(f"{operation_id}: {status} response lacks wire-major header")
        if header.get("required") is not True:
            raise ContractError(
                f"{operation_id}: {status} response wire-major header must be required"
            )
        if not is_wire_major_schema(resolve_reference(document, header.get("schema", {}))):
            raise ContractError(f"{operation_id}: {status} response header must be wire major 1")


def assert_request_constraints(document: dict[str, Any], operation: dict[str, Any]) -> None:
    operation_id = operation["operationId"]
    parameters = parameter_map(document, operation)
    request_body = resolve_reference(document, operation.get("requestBody", {}))
    content = request_body.get("content", {})
    for media in content.values():
        schema = resolve_reference(document, media.get("schema", {}))
        if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
            raise ContractError(f"{operation_id}: request body must reject unknown fields")
    if content.get("application/json") and operation["x-method"] == "post":
        if "header:idempotency-key" not in parameters:
            raise ContractError(f"{operation_id}: JSON POST requires Idempotency-Key")

    security = operation.get("security", document.get("security", []))
    tags = set(operation.get("tags", []))
    is_service_path = "/service/" in operation["x-path"]
    has_service_auth = any("ServiceBearerAuth" in requirement for requirement in security)
    has_consumer_auth = any(
        "ConsumerOrderOwnershipCookie" in requirement for requirement in security
    )
    if is_service_path:
        if not has_service_auth or has_consumer_auth or tags != {"service"}:
            raise ContractError(f"{operation_id}: service path requires service-only auth and tag")
    elif not has_consumer_auth or has_service_auth or tags != {"consumer"}:
        raise ContractError(f"{operation_id}: consumer path requires consumer-only auth and tag")


def assert_special_invariants(document: dict[str, Any]) -> None:
    schemas = document.get("components", {}).get("schemas", {})
    for name in CONSUMER_ALLOWLIST_SCHEMAS:
        schema = schemas.get(name, {})
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ContractError(f"{name}: safety-critical DTO must be a producer allowlist")

    upload = resolve_reference(document, schemas.get("UploadAttachmentRequest", {}))
    if "order_id" not in upload.get("required", []):
        raise ContractError("UploadAttachmentRequest: order_id binding is required")

    feed = resolve_reference(document, schemas.get("ServiceRequestFeed", {}))
    if not {"high_watermark", "next_cursor"}.issubset(feed.get("required", [])):
        raise ContractError("ServiceRequestFeed: high_watermark and next_cursor are required")
    next_cursor = resolve_reference(document, feed.get("properties", {}).get("next_cursor", {}))
    if next_cursor.get("type") == "null" or "anyOf" in next_cursor:
        raise ContractError("ServiceRequestFeed: next_cursor must always be non-null")

    error = resolve_reference(document, schemas.get("Error", {}))
    if "recovery_action" not in error.get("required", []):
        raise ContractError("Error: recovery_action is required for deterministic recovery")


def content_schema_pointer(
    document: dict[str, Any],
    container: dict[str, Any],
    container_pointer: str | None,
    content_type: str,
) -> tuple[str, dict[str, Any]]:
    resolved = resolve_reference(document, container)
    media = resolved.get("content", {}).get(content_type)
    if media is None:
        raise ContractError(f"declared operation lacks {content_type} content")
    schema_value = media.get("schema", {})
    if "$ref" in schema_value:
        pointer = schema_value["$ref"]
    elif container_pointer:
        pointer = f"{container_pointer}/content/{content_type.replace('/', '~1')}/schema"
    else:
        raise ContractError(
            "inline operation schemas must use a manifest-addressable response component"
        )
    return pointer, resolve_reference(document, schema_value)


def fixture_schema(
    document: dict[str, Any], operation: dict[str, Any], entry: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    representation = entry.get("representation", "json")
    if entry["kind"] == "request":
        if representation != "json":
            raise ContractError(f"{entry['fixture_id']}: only JSON request fixtures are supported")
        request_body = operation.get("requestBody", {})
        return content_schema_pointer(
            document,
            request_body,
            request_body.get("$ref"),
            "application/json",
        )

    status = str(entry.get("http_status", ""))
    if status not in operation.get("responses", {}):
        raise ContractError(
            f"{entry['fixture_id']}: {status} not declared for {operation['operationId']}"
        )
    response = operation["responses"][status]
    if entry.get("representation", "json") == "binary":
        resolved_response = resolve_reference(document, response)
        available = resolved_response.get("content", {})
        content_type = entry.get("media_type", "application/octet-stream")
        if content_type not in available:
            content_type = "application/octet-stream"
    else:
        content_type = "application/json"
    return content_schema_pointer(document, response, response.get("$ref"), content_type)


def schema_validator(document: dict[str, Any], schema: dict[str, Any]) -> Draft202012Validator:
    # Component refs are rooted at the OpenAPI document, not the extracted schema.
    schema_with_components = {**schema, "components": document.get("components", {})}
    return Draft202012Validator(schema_with_components, format_checker=FormatChecker())


def validate_binary_fixture(path: Path, entry: dict[str, Any]) -> None:
    data = path.read_bytes()
    expected_size = entry.get("size_bytes")
    expected_digest = entry.get("sha256")
    if len(data) != expected_size:
        raise ContractError(f"{entry['fixture_id']}: binary size mismatch")
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise ContractError(f"{entry['fixture_id']}: binary SHA-256 mismatch")
    if entry["expected_valid"] and len(data) > 5 * 1024 * 1024:
        raise ContractError(f"{entry['fixture_id']}: valid binary exceeds 5 MiB")
    media_type = entry.get("media_type")
    valid_magic = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }.get(media_type, False)
    if entry["expected_valid"] and not valid_magic:
        raise ContractError(f"{entry['fixture_id']}: binary magic bytes do not match {media_type}")
    if not entry["expected_valid"] and valid_magic and len(data) <= 5 * 1024 * 1024:
        raise ContractError(f"{entry['fixture_id']}: invalid binary unexpectedly passed")


def validate_error_semantics(operation_id: str, status: int, fixture: dict[str, Any]) -> None:
    error = fixture.get("error", {})
    code = error.get("code")
    if code in ERROR_STATUSES and status not in ERROR_STATUSES[code]:
        raise ContractError(f"{code}: invalid HTTP status {status}")
    if operation_id == "createServiceTicket" and status >= 429:
        if error.get("retryable") is not False or error.get("recovery_action") != "LOOKUP_FIRST":
            raise ContractError("createServiceTicket uncertain errors must require LOOKUP_FIRST")
    if operation_id == "getServiceTicketByIdempotencyKey" and status == 404:
        if error.get("retryable") is not False:
            raise ContractError("idempotency lookup 404 cannot be an immediate-retry signal")
        if error.get("recovery_action") != "RETRY_CREATE_WITH_SAME_KEY":
            raise ContractError("idempotency lookup 404 must require original-key recovery")


def validate_fixtures(
    bundle: Path,
    document: dict[str, Any],
    manifest: dict[str, Any],
    operation_map: dict[str, dict[str, Any]],
    product_text: str,
) -> None:
    entries = manifest.get("fixtures")
    if not isinstance(entries, list) or not entries:
        raise ContractError("fixtures/manifest.json: fixtures must be a non-empty list")

    fixture_ids: set[str] = set()
    referenced_paths: set[Path] = set()
    successful_operations: set[str] = set()
    known_requirements = set(REQUIREMENT_ID.findall(product_text))
    known_acceptance = set(ACCEPTANCE_ID.findall(product_text))

    for entry in entries:
        fixture_id = entry.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise ContractError("fixture entry lacks fixture_id")
        if fixture_id in fixture_ids:
            raise ContractError(f"duplicate fixture_id: {fixture_id}")
        fixture_ids.add(fixture_id)

        operation_id = entry.get("operation_id")
        if operation_id not in operation_map:
            raise ContractError(f"{fixture_id}: unknown operation_id {operation_id}")
        operation = operation_map[operation_id]
        if entry.get("kind") not in {"request", "response"}:
            raise ContractError(f"{fixture_id}: kind must be request or response")
        if not isinstance(entry.get("expected_valid"), bool):
            raise ContractError(f"{fixture_id}: expected_valid must be boolean")

        requirement_ids = entry.get("requirement_ids", [])
        acceptance_ids = entry.get("acceptance_ids", [])
        if not requirement_ids or not acceptance_ids:
            raise ContractError(f"{fixture_id}: requirement and acceptance IDs are required")
        unknown_ids = (set(requirement_ids) - known_requirements) | (
            set(acceptance_ids) - known_acceptance
        )
        if unknown_ids:
            raise ContractError(f"{fixture_id}: unknown product IDs {sorted(unknown_ids)}")
        if not set(requirement_ids).issubset(operation["x-requirement-ids"]):
            raise ContractError(f"{fixture_id}: requirement IDs not declared by {operation_id}")

        relative_path = Path(entry.get("path", ""))
        fixture_path = bundle / "fixtures" / relative_path
        if relative_path.is_absolute() or ".." in relative_path.parts or not fixture_path.is_file():
            raise ContractError(f"{fixture_id}: unsafe or missing fixture path {relative_path}")
        referenced_paths.add(fixture_path.resolve())

        expected_pointer, schema = fixture_schema(document, operation, entry)
        if entry.get("schema") != expected_pointer:
            raise ContractError(
                f"{fixture_id}: schema pointer {entry.get('schema')} does not match "
                f"operation schema {expected_pointer}"
            )

        representation = entry.get("representation", "json")
        if representation == "binary":
            if entry["kind"] != "response":
                raise ContractError(f"{fixture_id}: binary fixtures must be responses")
            validate_binary_fixture(fixture_path, entry)
        elif representation == "json":
            fixture = load_json(fixture_path)
            validator = schema_validator(document, schema)
            errors = sorted(validator.iter_errors(fixture), key=lambda error: list(error.path))
            if entry["expected_valid"] and errors:
                messages = "; ".join(error.message for error in errors[:3])
                raise ContractError(f"{fixture_id}: expected valid fixture: {messages}")
            if not entry["expected_valid"] and not errors:
                raise ContractError(f"{fixture_id}: invalid fixture unexpectedly passed")
            if entry["kind"] == "response" and entry["expected_valid"]:
                status = int(entry["http_status"])
                if expected_pointer == "#/components/schemas/ErrorResponse":
                    validate_error_semantics(operation_id, status, fixture)
        else:
            raise ContractError(f"{fixture_id}: unsupported representation {representation}")

        if entry["kind"] == "response" and entry["expected_valid"]:
            status = int(entry["http_status"])
            if 200 <= status < 300:
                successful_operations.add(operation_id)

    fixture_root = (bundle / "fixtures").resolve()
    actual_paths = {
        path.resolve()
        for path in fixture_root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    unreferenced = actual_paths - referenced_paths
    if unreferenced:
        names = sorted(str(path.relative_to(fixture_root)) for path in unreferenced)
        raise ContractError(f"fixtures not referenced by manifest: {names}")

    missing_success = set(operation_map) - successful_operations
    if missing_success:
        raise ContractError(f"operations lacking success fixtures: {sorted(missing_success)}")


def contract_checksums(bundle: Path) -> str:
    lines: list[str] = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(bundle).as_posix()}")
    return "\n".join(lines) + "\n"


def validate_checksums(bundle: Path) -> None:
    checksum_path = bundle / "SHA256SUMS"
    if not checksum_path.is_file():
        raise ContractError("SHA256SUMS is missing; run --write-checksums explicitly")
    actual = checksum_path.read_text(encoding="utf-8")
    expected = contract_checksums(bundle)
    if actual != expected:
        raise ContractError("SHA256SUMS is stale; update it explicitly after contract changes")


def validate_bundle(
    bundle: Path = DEFAULT_BUNDLE,
    product_doc: Path = DEFAULT_PRODUCT_DOC,
    check_checksums: bool = True,
) -> None:
    document = load_json(bundle / "openapi.json")
    manifest = load_json(bundle / "fixtures" / "manifest.json")
    product_text = product_doc.read_text(encoding="utf-8")

    if not document.get("openapi", "").startswith("3.1."):
        raise ContractError("contract must use OpenAPI 3.1.x")
    version = document.get("info", {}).get("version")
    if version != "1.0.0" or manifest.get("contract_version") != version:
        raise ContractError("OpenAPI and fixture manifest must both use package version 1.0.0")

    try:
        validate_openapi(document)
    except Exception as exc:
        raise ContractError(f"OpenAPI validation failed: {exc}") from exc

    operation_map = operations(document)
    security_schemes = document.get("components", {}).get("securitySchemes", {})
    consumer_cookie = security_schemes.get("ConsumerOrderOwnershipCookie", {})
    if consumer_cookie.get("name") != "epig_cart_{market}":
        raise ContractError("consumer cookie must follow E-pig's epig_cart_{market} template")
    assert_special_invariants(document)

    known_requirements = set(REQUIREMENT_ID.findall(product_text))
    for operation_id, operation in operation_map.items():
        requirement_ids = operation.get("x-requirement-ids")
        if not isinstance(requirement_ids, list) or not requirement_ids:
            raise ContractError(f"{operation_id}: x-requirement-ids must be non-empty")
        unknown = set(requirement_ids) - known_requirements
        if unknown:
            raise ContractError(f"{operation_id}: unknown requirement IDs {sorted(unknown)}")
        assert_contract_headers(document, operation)
        assert_request_constraints(document, operation)

    validate_fixtures(bundle, document, manifest, operation_map, product_text)
    if check_checksums:
        validate_checksums(bundle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--product-doc", type=Path, default=DEFAULT_PRODUCT_DOC)
    parser.add_argument(
        "--write-checksums",
        action="store_true",
        help="Explicitly refresh SHA256SUMS after all semantic checks pass",
    )
    args = parser.parse_args()
    try:
        validate_bundle(args.bundle, args.product_doc, check_checksums=not args.write_checksums)
        if args.write_checksums:
            (args.bundle / "SHA256SUMS").write_text(
                contract_checksums(args.bundle), encoding="utf-8", newline="\n"
            )
            validate_checksums(args.bundle)
    except ContractError as exc:
        print(f"after-sales contract: FAIL\n{exc}")
        return 1
    print("after-sales contract: OK (OpenAPI, fixtures, IDs, headers and write constraints)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
