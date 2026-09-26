#!/usr/bin/env python3
# Copyright (c) 2026 Michael LeMay
# SPDX-License-Identifier: MIT
"""Compare broad and narrowed USPTO patent queries."""

from __future__ import annotations

from patents_common import (
    APPLICANT_QUERY,
    ASSIGNEE_QUERY,
    BROAD_INVENTOR_QUERY,
    NARROW_QUERY,
    STRUCTURED_INVENTOR_QUERY,
    UTILITY_QUERY,
    UsptoClient,
    digits_only,
    inventor_names,
    is_utility,
    listings_by_line,
    mentions_intel,
    names_from_record,
    record_entry,
    record_has_michael_lemay,
    require_api_key,
)

QUERIES = [
    ("broad inventor", BROAD_INVENTOR_QUERY),
    ("structured inventor", STRUCTURED_INVENTOR_QUERY),
    ("structured + utility", UTILITY_QUERY),
    ("structured + Intel applicant", APPLICANT_QUERY),
    ("structured + Intel assignee", ASSIGNEE_QUERY),
    ("combined narrow query", NARROW_QUERY),
]


def main() -> int:
    client = UsptoClient(require_api_key())
    results = {
        label: client.collect_records(query, label, max_records=None)
        for label, query in QUERIES
    }
    result_listings = {
        label: listings_by_line(records) for label, records in results.items()
    }

    valid_listings: dict[str, dict] = {}
    non_listable_types: dict[str, int] = {}
    assignment_cache: dict[str, list[str]] = {}
    for record in results["broad inventor"]:
        if not record_has_michael_lemay(record) or not is_utility(record):
            continue
        application = digits_only(record.get("applicationNumberText") or "")
        local_names = names_from_record(record)
        if not mentions_intel(local_names):
            if not application:
                continue
            if application not in assignment_cache:
                assignment_cache[application] = client.assignment_names(application)
            if not mentions_intel(assignment_cache[application]):
                continue

        entry = record_entry(record)
        if entry:
            valid_listings[entry.line] = record
        else:
            metadata = record.get("applicationMetaData") or {}
            kind = metadata.get("applicationTypeLabelName") or "<unknown>"
            non_listable_types[kind] = non_listable_types.get(kind, 0) + 1

    print("\nQuery matrix")
    print(f"  client-validated US-listing baseline: {len(valid_listings)}")
    for label, _ in QUERIES:
        print(
            f"  {label}: {len(results[label])} records, "
            f"{len(result_listings[label])} possible US listings"
        )
    if non_listable_types:
        details = ", ".join(
            f"{kind}={count}" for kind, count in sorted(non_listable_types.items())
        )
        print(f"  client-valid records without a US listing: {details}")

    missing = sorted(
        set(valid_listings) - set(result_listings["combined narrow query"])
    )
    print(f"\nValid US listings missing from narrow query: {len(missing)}")
    for line in missing:
        record = valid_listings[line]
        metadata = record.get("applicationMetaData") or {}
        application = digits_only(record.get("applicationNumberText") or "")
        if application and application not in assignment_cache:
            assignment_cache[application] = client.assignment_names(application)
        assignees = assignment_cache.get(application, [])

        print(f"\n  {line.removeprefix('. ')}")
        print(f"    inventors: {inventor_names(record) or ['<none returned>']}")
        print(
            "    type: "
            f"{metadata.get('applicationTypeLabelName') or '<none returned>'}"
        )
        print(f"    applicants: {names_from_record(record) or ['<none returned>']}")
        print(f"    assignees: {assignees or ['<none returned>']}")
        for label, _ in QUERIES[1:]:
            matched = line in result_listings[label]
            print(f"    matched {label}: {'yes' if matched else 'no'}")

        if line not in result_listings["structured inventor"]:
            reason = "structured inventor fields"
        elif (
            line not in result_listings["structured + Intel applicant"]
            and line not in result_listings["structured + Intel assignee"]
        ):
            reason = "Intel applicant/assignee indexing"
        else:
            reason = "combined-query grouping or parser behavior"
        print(f"    first likely exclusion: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
