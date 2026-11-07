"""Validate the local E-pig synthetic demo policy bundle without network access."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.retrieval.policy import load_policy_manifest
from app.retrieval.unstructured_parser import list_knowledge_files

_MARKETS = {"au", "us", "sg"}
_TOPICS = {"returns", "refunds", "delivery", "cancellation", "faults-missing", "warranty"}
_CATEGORIES = {"mattress", "furniture", "homewares", "bedding"}
_SOURCE_LABEL = "E-pig 内部自编演示政策"


def validate_policy_pack(knowledge_dir: Path) -> tuple[int, int]:
    documents = load_policy_manifest(knowledge_dir)
    if len(documents) != 18:
        raise ValueError(f"E-pig policy manifest must contain 18 documents, got {len(documents)}")
    policy_files = {
        path.relative_to(knowledge_dir).as_posix()
        for path in list_knowledge_files(knowledge_dir)
        if path.relative_to(knowledge_dir).as_posix().startswith("epig/")
    }
    manifest_paths = set(documents)
    if policy_files != manifest_paths:
        raise ValueError(
            "policy files and manifest differ: "
            f"unregistered={sorted(policy_files - manifest_paths)}, "
            f"missing={sorted(manifest_paths - policy_files)}"
        )
    expected_paths = {f"epig/{market}/{topic}.md" for market in _MARKETS for topic in _TOPICS}
    if manifest_paths != expected_paths:
        raise ValueError("E-pig pack must cover six topics in each of AU, US and SG")

    for relative_path, document in documents.items():
        if document.brand != "epig" or document.market not in _MARKETS:
            raise ValueError(f"invalid E-pig brand/market scope: {relative_path}")
        if document.status != "demo" or document.source_type != "internal_demo":
            raise ValueError(f"E-pig policies must remain internal demo material: {relative_path}")
        if document.source_urls or document.source_label != _SOURCE_LABEL:
            raise ValueError(f"invalid E-pig internal source metadata: {relative_path}")
        if document.source_path != relative_path:
            raise ValueError(f"E-pig source path mismatch: {relative_path}")
        if set(document.product_categories) != _CATEGORIES:
            raise ValueError(f"incomplete E-pig product categories: {relative_path}")
        text = (knowledge_dir / relative_path).read_text(encoding="utf-8")
        if not text.startswith("# E-pig"):
            raise ValueError(f"policy document lacks an E-pig H1 title: {relative_path}")
        required_disclosures = ("内部自编演示", "官方政策", "生产批准", "真实订单")
        if any(value not in text for value in required_disclosures):
            raise ValueError(f"policy document lacks demo scope disclosures: {relative_path}")
        forbidden = ("http://", "https://", "Koala", "White Glove", "Premium Delivery")
        if any(value in text for value in forbidden):
            raise ValueError(
                f"policy document contains external or legacy brand material: {relative_path}"
            )
        if document.requires_human_review and "## 人工复核" not in text:
            raise ValueError(f"review section missing: {relative_path}")

    review_count = sum(document.requires_human_review for document in documents.values())
    return len(documents), review_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--knowledge-dir", type=Path, default=Path("knowledge"))
    args = parser.parse_args()
    count, review_count = validate_policy_pack(args.knowledge_dir)
    print(
        f"E-pig synthetic demo policy pack: OK ({count} AU/US/SG documents; "
        f"{review_count} require query-specific human review)"
    )
    print(
        "Static internal-source checks only; no external URLs or production approval are claimed."
    )


if __name__ == "__main__":
    main()
