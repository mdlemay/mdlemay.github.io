#!/usr/bin/env python3
# Copyright (c) 2026 Michael LeMay
# SPDX-License-Identifier: MIT
"""Add newly published Michael LeMay/Intel patents to patents.adoc."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from patents_common import (
    NewEntry,
    UsptoClient,
    application_entry,
    compact_publication_id,
    digits_only,
    format_application_number,
    is_utility,
    issued_entry,
    record_has_michael_lemay,
    require_api_key,
)

GOOGLE_PATENT_RE = re.compile(r"patents\.google\.com/patent/(US[A-Z0-9]+)/", re.I)
ISSUED_URL_RE = re.compile(r"patents\.google\.com/patent/US(\d+)B\d/", re.I)
APPLICATION_URL_RE = re.compile(r"patents\.google\.com/patent/US(\d{11})A\d/", re.I)
ISSUED_LABEL_RE = re.compile(r"\[([\d,]+)\s+\((\d{4})\):")
APP_LABEL_RE = re.compile(r"\[(\d{1,2}/\d{3},\d{3}):")
ISSUED_HEADING = "== Issued patents"
APPS_HEADING = "== Published patent applications"


@dataclass(frozen=True)
class Catalog:
    patent_numbers: set[str]
    application_numbers: set[str]
    publication_ids: set[str]


def parse_catalog(text: str) -> Catalog:
    patent_numbers = {
        digits_only(match.group(1)) for match in ISSUED_LABEL_RE.finditer(text)
    }
    application_numbers = {
        digits_only(match.group(1)) for match in APP_LABEL_RE.finditer(text)
    }
    publication_ids = {
        publication_id
        for match in GOOGLE_PATENT_RE.finditer(text)
        if (publication_id := compact_publication_id(match.group(1)))
    }
    patent_numbers.update(
        match.group(1)
        for publication_id in publication_ids
        if (match := re.fullmatch(r"US(\d{6,8})B\d", publication_id))
    )
    return Catalog(patent_numbers, application_numbers, publication_ids)


def listed_application_matches(entry: NewEntry, catalog: Catalog) -> bool:
    return (
        bool(entry.application_digits)
        and entry.application_digits in catalog.application_numbers
    ) or (
        bool(entry.publication_id)
        and entry.publication_id in catalog.publication_ids
    )


def removal_for_first_issuance(
    issued: NewEntry, catalog: Catalog
) -> NewEntry | None:
    if not listed_application_matches(issued, catalog):
        return None
    display = (
        format_application_number(issued.application_digits)
        if issued.application_digits
        else issued.publication_id
    )
    return NewEntry(
        kind="removed_application",
        line="",
        sort_key=issued.application_digits or issued.publication_id,
        summary=f"{display} (now issued as {issued.summary.split(':', 1)[0].strip()})",
        application_digits=issued.application_digits,
        publication_id=issued.publication_id,
    )


def select_new_entries(
    records: list[dict], catalog: Catalog, client: UsptoClient
) -> list[NewEntry]:
    entries: list[NewEntry] = []
    seen_lines: set[str] = set()
    seen_removals: set[tuple[str, str]] = set()

    for record in records:
        if (
            not is_utility(record)
            or not record_has_michael_lemay(record)
            or not client.assigned_to_intel(record)
        ):
            continue

        issued = issued_entry(record)
        if issued:
            patent_number = digits_only(
                (record.get("applicationMetaData") or {}).get("patentNumber") or ""
            )
            if patent_number not in catalog.patent_numbers:
                if issued.line not in seen_lines:
                    entries.append(issued)
                    seen_lines.add(issued.line)
                removal = removal_for_first_issuance(issued, catalog)
                removal_key = (
                    (removal.application_digits, removal.publication_id)
                    if removal
                    else None
                )
                if removal and removal_key not in seen_removals:
                    entries.append(removal)
                    seen_removals.add(removal_key)
            continue

        application = application_entry(record)
        if (
            application
            and not listed_application_matches(application, catalog)
            and application.line not in seen_lines
        ):
            entries.append(application)
            seen_lines.add(application.line)
    return entries


def application_line_keys(line: str) -> tuple[str, str]:
    app_match = APP_LABEL_RE.search(line)
    publication_match = GOOGLE_PATENT_RE.search(line)
    application = digits_only(app_match.group(1)) if app_match else ""
    publication = (
        compact_publication_id(publication_match.group(1))
        if publication_match
        else ""
    )
    return application, publication or ""


def remove_application_lines(section: str, removals: list[NewEntry]) -> str:
    applications = {
        entry.application_digits for entry in removals if entry.application_digits
    }
    publications = {
        entry.publication_id
        for entry in removals
        if entry.publication_id and not entry.application_digits
    }
    kept = []
    for line in section.splitlines():
        application, publication = application_line_keys(line)
        if application in applications or (
            not application and publication in publications
        ):
            continue
        kept.append(line)
    return preserve_final_newline(section, kept)


def insert_lines(section: str, new_lines: list[str]) -> str:
    lines = section.splitlines()
    position = next(
        (index for index, line in enumerate(lines) if line.strip()), len(lines)
    )
    return preserve_final_newline(
        section, lines[:position] + new_lines + lines[position:]
    )


def preserve_final_newline(original: str, lines: list[str]) -> str:
    text = "\n".join(lines)
    return text + "\n" if original.endswith("\n") and not text.endswith("\n") else text


def sort_listing_section(section: str, pattern: re.Pattern[str]) -> str:
    lines = section.splitlines()
    positions_and_listings = [
        (index, int(match.group(1)), line)
        for index, line in enumerate(lines)
        if (match := pattern.search(line))
    ]
    sorted_lines = sorted(
        positions_and_listings, key=lambda item: item[1], reverse=True
    )
    for (position, _, _), (_, _, line) in zip(
        positions_and_listings, sorted_lines
    ):
        lines[position] = line
    return preserve_final_newline(section, lines)


def split_sections(text: str) -> tuple[str, str, str, str]:
    issued_at = text.find(ISSUED_HEADING)
    apps_at = text.find(APPS_HEADING)
    if issued_at < 0 or apps_at < issued_at:
        raise RuntimeError("patents.adoc is missing the expected section headings.")
    issued_end = issued_at + len(ISSUED_HEADING)
    apps_end = apps_at + len(APPS_HEADING)
    return (
        text[:issued_end],
        text[issued_end:apps_at],
        text[apps_at:apps_end],
        text[apps_end:],
    )


def sort_patent_listings(text: str) -> str:
    prefix, issued, apps_heading, applications = split_sections(text)
    return (
        prefix
        + sort_listing_section(issued, ISSUED_URL_RE)
        + apps_heading
        + sort_listing_section(applications, APPLICATION_URL_RE)
    )


def apply_updates(text: str, entries: list[NewEntry]) -> str:
    prefix, issued, apps_heading, applications = split_sections(text)
    issued = insert_lines(
        issued, [entry.line for entry in entries if entry.kind == "issued"]
    )
    applications = remove_application_lines(
        applications,
        [entry for entry in entries if entry.kind == "removed_application"],
    )
    applications = insert_lines(
        applications,
        [entry.line for entry in entries if entry.kind == "application"],
    )
    return sort_patent_listings(prefix + issued + apps_heading + applications)


def set_github_output(name: str, value: str) -> None:
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def write_pr_body(path: Path | None, entries: list[NewEntry]) -> None:
    sections = [
        (
            "New issued patents",
            [entry for entry in entries if entry.kind == "issued"],
        ),
        (
            "New published applications",
            [entry for entry in entries if entry.kind == "application"],
        ),
        (
            "Removed published applications",
            [entry for entry in entries if entry.kind == "removed_application"],
        ),
    ]
    lines = [
        "Monthly USPTO scan found changes for `patents.adoc`.",
        "",
        "Please review titles, kind codes, and links before merging.",
        "",
    ]
    for heading, matching_entries in sections:
        if matching_entries:
            lines.append(f"## {heading}")
            lines.extend(f"- {entry.summary}" for entry in matching_entries)
            lines.append("")
    lines.append("Source: [USPTO Open Data Portal](https://data.uspto.gov/).")
    body = "\n".join(lines) + "\n"
    if path:
        path.write_text(body, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(body)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    patents_path = Path(os.environ.get("PATENTS_FILE") or root / "patents.adoc")
    original = patents_path.read_text(encoding="utf-8")
    catalog = parse_catalog(original)

    if "--parse-only" in sys.argv:
        print(f"Catalogued {len(catalog.patent_numbers)} issued patent numbers.")
        print(f"Catalogued {len(catalog.application_numbers)} application numbers.")
        print(f"Catalogued {len(catalog.publication_ids)} Google Patents identifiers.")
        return 0

    if "--sort-only" in sys.argv:
        sorted_text = sort_patent_listings(original)
        if sorted_text == original:
            print(f"{patents_path} is already in reverse-chronological order.")
        else:
            patents_path.write_text(sorted_text, encoding="utf-8", newline="\n")
            print(f"Sorted {patents_path} in reverse-chronological order.")
        return 0

    client = UsptoClient(require_api_key())
    entries = select_new_entries(client.collect_records(), catalog, client)
    if not entries:
        print("No new US Intel patents or applications to add.")
        set_github_output("updated", "false")
        return 0

    updated = apply_updates(original, entries)
    patents_path.write_text(updated, encoding="utf-8", newline="\n")
    write_pr_body(
        Path(body_path) if (body_path := os.environ.get("PATENT_PR_BODY_PATH")) else None,
        entries,
    )
    set_github_output("updated", "true")
    added = sum(entry.kind != "removed_application" for entry in entries)
    removed = sum(entry.kind == "removed_application" for entry in entries)
    print(f"Updated {patents_path}: added {added}, removed {removed}.")
    for entry in entries:
        print(f"  - {entry.kind}: {entry.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
