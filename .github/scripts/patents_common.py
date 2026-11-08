# Copyright (c) 2026 Michael LeMay
# SPDX-License-Identifier: MIT
"""Shared USPTO API, query, and patent-record helpers."""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Change these settings to reuse the scanner on another site.
INVENTOR_FIRST_NAME = "Michael"
# The first spelling is used in the structured USPTO query. Later spellings are
# extra inventorNameText forms for the broad diagnostic query.
INVENTOR_LAST_NAMES = ("LeMay", "Lemay")
# Applicant or assignee. Leave empty to keep every utility filing for the inventor.
ORGANIZATION_NAME = "Intel"
USER_AGENT = "mdlemay.github.io patent sync (https://github.com/mdlemay/mdlemay.github.io)"
PATENTS_PATH = "patents.adoc"
ISSUED_HEADING = "== Issued patents"
APPLICATIONS_HEADING = "== Published patent applications"
ISSUED_KIND_CODE = "B2"

ODP_BASE = "https://api.uspto.gov"
SEARCH_PATH = "/api/v1/patent/applications/search"
ASSIGNMENT_PATH = "/api/v1/patent/applications/{application}/assignment"
PAGE_SIZE = 25
MAX_RECORDS = 1000

_INVENTOR_LAST_NAME = INVENTOR_LAST_NAMES[0]
BROAD_INVENTOR_QUERY = " OR ".join(
    [
        *[
            f"applicationMetaData.inventorBag.inventorNameText:{name}"
            for name in INVENTOR_LAST_NAMES
        ],
        f"applicationMetaData.inventorBag.lastName:{_INVENTOR_LAST_NAME}",
    ]
)
STRUCTURED_INVENTOR_QUERY = (
    f"(applicationMetaData.inventorBag.firstName:{INVENTOR_FIRST_NAME}*"
    f" AND applicationMetaData.inventorBag.lastName:{_INVENTOR_LAST_NAME})"
)
UTILITY_QUERY = (
    f"{STRUCTURED_INVENTOR_QUERY}"
    " AND applicationMetaData.applicationTypeLabelName:Utility"
)
if ORGANIZATION_NAME:
    APPLICANT_CLAUSE = (
        f"(applicationMetaData.firstApplicantName:{ORGANIZATION_NAME}*"
        f" OR applicationMetaData.applicantBag.applicantNameText:{ORGANIZATION_NAME}*)"
    )
    ASSIGNEE_CLAUSE = (
        f"assignmentBag.assigneeBag.assigneeNameText:{ORGANIZATION_NAME}*"
    )
    APPLICANT_QUERY = f"{STRUCTURED_INVENTOR_QUERY} AND {APPLICANT_CLAUSE}"
    ASSIGNEE_QUERY = f"{STRUCTURED_INVENTOR_QUERY} AND {ASSIGNEE_CLAUSE}"
    NARROW_QUERY = (
        f"{STRUCTURED_INVENTOR_QUERY} AND ({APPLICANT_CLAUSE} OR {ASSIGNEE_CLAUSE})"
    )
    SEARCH_QUERY = NARROW_QUERY
else:
    APPLICANT_CLAUSE = ""
    ASSIGNEE_CLAUSE = ""
    APPLICANT_QUERY = ""
    ASSIGNEE_QUERY = ""
    NARROW_QUERY = ""
    SEARCH_QUERY = UTILITY_QUERY
SEARCH_FIELDS = [
    "applicationNumberText",
    "applicationMetaData.applicationTypeCode",
    "applicationMetaData.applicationTypeLabelName",
    "applicationMetaData.earliestPublicationDate",
    "applicationMetaData.earliestPublicationNumber",
    "applicationMetaData.filingDate",
    "applicationMetaData.firstApplicantName",
    "applicationMetaData.firstInventorName",
    "applicationMetaData.grantDate",
    "applicationMetaData.inventionTitle",
    "applicationMetaData.inventorBag",
    "applicationMetaData.applicantBag",
    "applicationMetaData.patentNumber",
]


@dataclass
class NewEntry:
    kind: str
    line: str
    sort_key: str
    summary: str
    application_digits: str = ""
    publication_id: str = ""


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z]", "", (value or "").casefold())


def format_patent_number(raw: str) -> str:
    return f"{int(digits_only(raw)):,}"


def format_application_number(raw: str) -> str:
    digits = digits_only(raw)
    if len(digits) == 8:
        return f"{digits[:2]}/{digits[2:5]},{digits[5:]}"
    if len(digits) == 7:
        return f"{digits[0]}/{digits[1:4]},{digits[4:]}"
    return (raw or "").strip()


def compact_publication_id(raw: str | None) -> str | None:
    if not raw:
        return None
    compact = re.sub(r"[\s-]", "", raw).upper()
    if compact.startswith("US"):
        return compact
    if re.fullmatch(r"\d{7,11}[A-Z]\d?", compact):
        return f"US{compact}"
    return None


def inventor_matches(inventor: dict) -> bool:
    last_names = {normalize_name(name) for name in INVENTOR_LAST_NAMES}
    first_name = normalize_name(INVENTOR_FIRST_NAME)

    def matches(last: str, first: str) -> bool:
        return last in last_names and bool(first_name) and first.startswith(first_name)

    last = normalize_name(inventor.get("lastName") or "")
    first = normalize_name(inventor.get("firstName") or "")
    if matches(last, first):
        return True

    full = inventor.get("inventorNameText") or inventor.get("preferredName") or ""
    if "," in full:
        last_part, first_part = [part.strip() for part in full.split(",", 1)]
        return matches(normalize_name(last_part), normalize_name(first_part))

    tokens = [normalize_name(part) for part in full.split() if normalize_name(part)]
    return bool(tokens) and matches(tokens[-1], tokens[0])


def record_has_inventor(record: dict) -> bool:
    meta = record.get("applicationMetaData") or {}
    if any(inventor_matches(inventor) for inventor in meta.get("inventorBag") or []):
        return True
    return inventor_matches(
        {"inventorNameText": meta.get("firstInventorName") or ""}
    )


def names_from_record(record: dict) -> list[str]:
    meta = record.get("applicationMetaData") or {}
    names = [meta.get("firstApplicantName") or ""]
    for applicant in meta.get("applicantBag") or []:
        names.extend(
            (
                applicant.get("applicantNameText") or "",
                applicant.get("preferredName") or "",
            )
        )
    for assignment in record.get("assignmentBag") or []:
        names.extend(
            assignee.get("assigneeNameText") or ""
            for assignee in assignment.get("assigneeBag") or []
        )
    return [name for name in names if name]


def inventor_names(record: dict) -> list[str]:
    meta = record.get("applicationMetaData") or {}
    names = []
    for inventor in meta.get("inventorBag") or []:
        full = inventor.get("inventorNameText") or " ".join(
            filter(None, (inventor.get("firstName"), inventor.get("lastName")))
        )
        if full:
            names.append(full)
    return names


def matches_organization(names: list[str]) -> bool:
    if not ORGANIZATION_NAME:
        return True
    pattern = rf"\b{re.escape(ORGANIZATION_NAME)}\b"
    return any(re.search(pattern, name, re.I) for name in names)


def is_utility(record: dict) -> bool:
    meta = record.get("applicationMetaData") or {}
    code = (meta.get("applicationTypeCode") or "").upper()
    label = (meta.get("applicationTypeLabelName") or "").casefold()
    return code not in {"DES", "PLT"} and label not in {"design", "plant"}


def sentence_case_title(title: str) -> str:
    cleaned = " ".join((title or "").split())
    if cleaned.isupper():
        return cleaned[:1] + cleaned[1:].lower()
    return cleaned


def application_title(title: str) -> str:
    return " ".join((title or "").split()).upper()


def year_from_date(value: str | None) -> str | None:
    return value[:4] if value and re.match(r"\d{4}", value) else None


def issued_entry(record: dict) -> NewEntry | None:
    meta = record.get("applicationMetaData") or {}
    digits = digits_only(meta.get("patentNumber") or "")
    year = year_from_date(meta.get("grantDate")) or year_from_date(
        meta.get("filingDate")
    )
    if not digits or not year:
        return None
    title = sentence_case_title(meta.get("inventionTitle") or "Untitled")
    display = format_patent_number(digits)
    return NewEntry(
        kind="issued",
        line=(
            f". https://patents.google.com/patent/US{digits}{ISSUED_KIND_CODE}/en"
            f"[{display} ({year}): {title}]"
        ),
        sort_key=f"{year}-{digits.zfill(8)}",
        summary=f"{display} ({year}): {title}",
        application_digits=digits_only(record.get("applicationNumberText") or ""),
        publication_id=compact_publication_id(
            meta.get("earliestPublicationNumber")
        )
        or "",
    )


def application_entry(record: dict) -> NewEntry | None:
    meta = record.get("applicationMetaData") or {}
    application_number = record.get("applicationNumberText") or ""
    publication_id = compact_publication_id(meta.get("earliestPublicationNumber"))
    if not application_number or not publication_id:
        return None
    title = application_title(meta.get("inventionTitle") or "UNTITLED")
    display = format_application_number(application_number)
    date_key = year_from_date(meta.get("earliestPublicationDate")) or "0000"
    return NewEntry(
        kind="application",
        line=f". https://patents.google.com/patent/{publication_id}/en[{display}: {title}]",
        sort_key=f"{date_key}-{digits_only(application_number).zfill(8)}",
        summary=f"{display}: {title}",
        application_digits=digits_only(application_number),
        publication_id=publication_id,
    )


def record_entry(record: dict) -> NewEntry | None:
    return issued_entry(record) or application_entry(record)


def listings_by_line(records: list[dict]) -> dict[str, dict]:
    return {
        entry.line: record
        for record in records
        if (entry := record_entry(record)) is not None
    }


def require_api_key() -> str:
    api_key = (os.environ.get("USPTO_ODP_API_KEY") or "").strip()
    if api_key:
        return api_key
    raise SystemExit(
        "Set USPTO_ODP_API_KEY to a USPTO Open Data Portal key "
        "(https://data.uspto.gov/apikey)."
    )


class UsptoClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-API-KEY": api_key,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }
        )
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=None,
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def request_json(
        self, method: str, path: str, payload: dict | None = None
    ) -> dict:
        try:
            response = self.session.request(
                method, f"{ODP_BASE}{path}", json=payload, timeout=60
            )
        except requests.RequestException as error:
            raise RuntimeError(f"USPTO ODP request failed: {error}") from error

        if response.status_code in {401, 403}:
            raise RuntimeError(
                f"USPTO ODP rejected the API key (HTTP {response.status_code}). "
                "Check USPTO_ODP_API_KEY and verify that the key is active at "
                "https://data.uspto.gov/apikey."
            )
        if not response.ok:
            raise RuntimeError(
                f"USPTO ODP {response.status_code} for {response.url}: "
                f"{response.text[:500]}"
            )
        try:
            return response.json()
        except requests.JSONDecodeError as error:
            raise RuntimeError(
                f"USPTO ODP returned invalid JSON for {response.url}."
            ) from error

    def collect_records(
        self,
        query: str = SEARCH_QUERY,
        label: str = "USPTO search",
        max_records: int | None = MAX_RECORDS,
    ) -> list[dict]:
        records: list[dict] = []
        offset = 0
        total = None
        while True:
            page = self.request_json(
                "POST",
                SEARCH_PATH,
                {
                    "q": query,
                    "fields": SEARCH_FIELDS,
                    "sort": [
                        {
                            "field": "applicationMetaData.filingDate",
                            "order": "desc",
                        }
                    ],
                    "pagination": {"offset": offset, "limit": PAGE_SIZE},
                },
            )
            if total is None:
                total = int(page.get("count") or page.get("totalNumFound") or 0)
                print(
                    f"{label} matched {total} file wrapper(s).", file=sys.stderr
                )
            bag = page.get("patentFileWrapperDataBag") or []
            if not bag:
                break
            records.extend(bag)
            offset += len(bag)
            if (
                (total and offset >= total)
                or (max_records is not None and offset >= max_records)
                or len(bag) < PAGE_SIZE
            ):
                break
            time.sleep(0.4)
        return records

    def assignment_names(self, application_number: str) -> list[str]:
        path = ASSIGNMENT_PATH.format(application=quote(application_number, safe=""))
        try:
            response = self.request_json("GET", path)
        except RuntimeError as error:
            if " 404 " in str(error):
                return []
            raise
        return [
            name
            for item in response.get("patentFileWrapperDataBag") or []
            for name in names_from_record(item)
        ]

    def assigned_to_organization(self, record: dict) -> bool:
        if matches_organization(names_from_record(record)):
            return True
        application = record.get("applicationNumberText")
        return bool(application) and matches_organization(
            self.assignment_names(application)
        )
