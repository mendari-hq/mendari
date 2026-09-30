"""OMOP CDM 5.4 as pack inputs, for population-level evaluation.

Research networks (OHDSI) keep patient data in the OMOP Common Data Model. This reads a
population from an OMOP database (SQLite or Postgres; tables may sit in a schema such as `cdm`)
as the clinical record that record packs take, one per person:

| OMOP | Record |
|---|---|
| `person`: birth date, `gender_concept_id` | `patient.age` at the screening date, `patient.sex` |
| `observation_period`: the last end date | `as_of`, the screening date (unless one is given) |
| `condition_occurrence` | `conditions`: ICD-10-CM source or SNOMED code, name, status, start |
| `measurement` | `observations`: the concept's code (LOINC) and name, value, unit, date |
| `drug_exposure` | `medications`: source name, concept (RxNorm) code, start, end, active or not |
| `note` | `notes`, in date order |

Codes come from the `concept` table (`concept_code`, `vocabulary_id`), as in any OMOP instance.
A drug exposure is active while it has no end date, or ends after the screening date;
otherwise its `stop_reason` (`completed` or `stopped`) is its status.

`write` does the reverse for synthetic records (`openhealth sandbox omop`), with local concept
ids (2,000,000,000 and up, the range OMOP reserves for them), so a dataset can make the round
trip and be checked in CI.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from typing import Any

from openhealth.core.db import Backend, Session, connect
from openhealth.errors import OpenHealthError
from openhealth.providers.criteria import _date, _items
from openhealth.standards.record_fhir import _ICD10_CODE, born

LOCK_KEY = 0x0E0D_0001
GENDER = {8507: "male", 8532: "female", 8521: "other", 8551: "unknown"}
GENDER_ID = {sex: concept for concept, sex in GENDER.items()}
LOCAL_CONCEPTS = 2_000_000_000

# The CDM 5.4 tables (and columns) this module uses; a real instance has many more.
SCHEMA = """
CREATE TABLE IF NOT EXISTS {prefix}person (
    person_id            INTEGER PRIMARY KEY,
    gender_concept_id    INTEGER NOT NULL,
    year_of_birth        INTEGER NOT NULL,
    month_of_birth       INTEGER,
    day_of_birth         INTEGER,
    person_source_value  VARCHAR(50)
);
CREATE TABLE IF NOT EXISTS {prefix}observation_period (
    observation_period_id          INTEGER PRIMARY KEY,
    person_id                      INTEGER NOT NULL,
    observation_period_start_date  DATE NOT NULL,
    observation_period_end_date    DATE NOT NULL,
    period_type_concept_id         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS {prefix}concept (
    concept_id        INTEGER PRIMARY KEY,
    concept_name      VARCHAR(255) NOT NULL,
    domain_id         VARCHAR(20) NOT NULL,
    vocabulary_id     VARCHAR(20) NOT NULL,
    concept_code      VARCHAR(50) NOT NULL
);
CREATE TABLE IF NOT EXISTS {prefix}condition_occurrence (
    condition_occurrence_id        INTEGER PRIMARY KEY,
    person_id                      INTEGER NOT NULL,
    condition_concept_id           INTEGER NOT NULL,
    condition_start_date           DATE,
    condition_type_concept_id      INTEGER NOT NULL,
    condition_status_source_value  VARCHAR(50),
    condition_source_value         VARCHAR(50),
    condition_source_concept_id    INTEGER
);
CREATE TABLE IF NOT EXISTS {prefix}measurement (
    measurement_id             INTEGER PRIMARY KEY,
    person_id                  INTEGER NOT NULL,
    measurement_concept_id     INTEGER NOT NULL,
    measurement_date           DATE,
    measurement_type_concept_id INTEGER NOT NULL,
    value_as_number            {real},
    unit_source_value          VARCHAR(50),
    measurement_source_value   VARCHAR(50)
);
CREATE TABLE IF NOT EXISTS {prefix}drug_exposure (
    drug_exposure_id             INTEGER PRIMARY KEY,
    person_id                    INTEGER NOT NULL,
    drug_concept_id              INTEGER NOT NULL,
    drug_exposure_start_date     DATE,
    drug_exposure_end_date       DATE,
    drug_type_concept_id         INTEGER NOT NULL,
    stop_reason                  VARCHAR(20),
    drug_source_value            VARCHAR(255)
);
CREATE TABLE IF NOT EXISTS {prefix}note (
    note_id               INTEGER PRIMARY KEY,
    person_id             INTEGER NOT NULL,
    note_date             DATE NOT NULL,
    note_type_concept_id  INTEGER NOT NULL,
    note_class_concept_id INTEGER NOT NULL,
    note_title            VARCHAR(250),
    note_text             TEXT NOT NULL,
    encoding_concept_id   INTEGER NOT NULL,
    language_concept_id   INTEGER NOT NULL
);
"""
EHR = 32817  # type concept: EHR


def open_database(url: str, *, schema: str = "", create: bool = False) -> Backend:
    """An OMOP database. Reading creates nothing; `create` (for synthetic data) makes the
    tables this module uses."""
    prefix = _prefix(schema)
    ddl = SCHEMA.replace("{prefix}", prefix) if create else "SELECT 1;"
    return connect(url, schema=ddl, migrate=lambda _: None, lock_key=LOCK_KEY)


def read(
    db: Backend,
    *,
    schema: str = "",
    as_of: date | None = None,
    limit: int | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """(person id, record) for each person, in person order."""
    t = _prefix(schema)
    with db.session(exclusive=False) as s:
        people = s.all(
            f"SELECT person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, "
            f"person_source_value FROM {t}person ORDER BY person_id"
            + (f" LIMIT {int(limit)}" if limit else "")
        )
        if not people:
            return
        ids = [p["person_id"] for p in people]
        ends = _grouped(
            s,
            f"SELECT person_id, observation_period_end_date AS end_date "
            f"FROM {t}observation_period WHERE person_id IN ({{ids}})",
            ids,
        )
        conditions = _grouped(
            s,
            f"SELECT c.person_id, c.condition_occurrence_id AS row_id, c.condition_start_date, "
            f"c.condition_status_source_value, c.condition_source_value, "
            f"sc.concept_code AS source_code, sc.concept_name AS source_name, "
            f"k.concept_code AS code, k.concept_name AS name FROM {t}condition_occurrence c "
            f"LEFT JOIN {t}concept sc ON sc.concept_id = c.condition_source_concept_id "
            f"LEFT JOIN {t}concept k ON k.concept_id = c.condition_concept_id "
            f"WHERE c.person_id IN ({{ids}})",
            ids,
        )
        measurements = _grouped(
            s,
            f"SELECT m.person_id, m.measurement_id AS row_id, m.measurement_date, "
            f"m.value_as_number, m.unit_source_value, m.measurement_source_value, "
            f"k.concept_code AS code, k.concept_name AS name FROM {t}measurement m "
            f"LEFT JOIN {t}concept k ON k.concept_id = m.measurement_concept_id "
            f"WHERE m.person_id IN ({{ids}})",
            ids,
        )
        drugs = _grouped(
            s,
            f"SELECT d.person_id, d.drug_exposure_id AS row_id, d.drug_exposure_start_date, "
            f"d.drug_exposure_end_date, d.stop_reason, d.drug_source_value, "
            f"k.concept_code AS code, k.vocabulary_id FROM {t}drug_exposure d "
            f"LEFT JOIN {t}concept k ON k.concept_id = d.drug_concept_id "
            f"WHERE d.person_id IN ({{ids}})",
            ids,
        )
        notes = _grouped(
            s,
            f"SELECT person_id, note_id AS row_id, note_date, note_text FROM {t}note "
            f"WHERE person_id IN ({{ids}})",
            ids,
        )
    for person in people:
        pid = person["person_id"]
        screening = as_of or max(
            (d for d in (_date(str(r["end_date"])) for r in ends[pid]) if d is not None),
            default=None,
        )
        if screening is None:
            raise OpenHealthError(f"person {pid} has no observation period: give a screening date")
        record: dict[str, Any] = {"as_of": screening.isoformat()}
        patient = _patient(person, screening)
        if patient:
            record["patient"] = patient
        record["conditions"] = [_condition(r) for r in _ordered(conditions[pid])]
        record["observations"] = [_measurement(r) for r in _ordered(measurements[pid])]
        record["medications"] = [_drug(r, screening) for r in _ordered(drugs[pid])]
        texts = [str(r["note_text"]) for r in sorted(notes[pid], key=_note_order)]
        if texts:
            record["notes"] = "\n\n".join(texts)
        yield str(person["person_source_value"] or pid), record


def write(
    db: Backend, records: Sequence[tuple[str, Mapping[str, Any]]], *, schema: str = ""
) -> int:
    """Synthetic records into OMOP tables (for the round trip): one person each."""
    t = _prefix(schema)
    concepts: dict[tuple[str, str], int] = {}
    counters: dict[str, int] = defaultdict(int)

    def concept(domain: str, vocabulary: str, code: str, name: str) -> int:
        key = (vocabulary, code)
        if key not in concepts:
            concepts[key] = LOCAL_CONCEPTS + len(concepts)
            s.all(
                f"INSERT INTO {t}concept (concept_id, concept_name, domain_id, vocabulary_id, "
                "concept_code) VALUES (?, ?, ?, ?, ?)",
                (concepts[key], name or code, domain, vocabulary, code),
            )
        return concepts[key]

    def next_id(table: str) -> int:
        counters[table] += 1
        return counters[table]

    with db.session(exclusive=True) as s:
        for number, (source_id, record) in enumerate(records, start=1):
            as_of = _date(record.get("as_of"))
            if as_of is None:
                raise OpenHealthError(f"{source_id}: a record needs an as_of date for OMOP")
            patient = record.get("patient") if isinstance(record.get("patient"), Mapping) else {}
            age = patient.get("age") if isinstance(patient, Mapping) else None
            birth = born(age, as_of) if isinstance(age, int) else None
            sex = patient.get("sex") if isinstance(patient, Mapping) else None
            s.all(
                f"INSERT INTO {t}person (person_id, gender_concept_id, year_of_birth, "
                "month_of_birth, day_of_birth, person_source_value) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    number,
                    GENDER_ID.get(str(sex), 0),
                    birth.year if birth else 0,  # 0: unknown (the record gave no age)
                    birth.month if birth else None,
                    birth.day if birth else None,
                    source_id,
                ),
            )
            s.all(
                f"INSERT INTO {t}observation_period (observation_period_id, person_id, "
                "observation_period_start_date, observation_period_end_date, "
                "period_type_concept_id) VALUES (?, ?, ?, ?, ?)",
                (number, number, "2000-01-01", as_of.isoformat(), EHR),
            )
            for item in _items(record.get("conditions")):
                code = str(item.get("code") or "")
                icd = bool(_ICD10_CODE.match(code))
                vocabulary = "ICD10CM" if icd else "SNOMED"
                source = concept("Condition", vocabulary, code, str(item.get("text") or ""))
                onset = _date(item.get("onset"))
                s.all(
                    f"INSERT INTO {t}condition_occurrence (condition_occurrence_id, person_id, "
                    "condition_concept_id, condition_start_date, condition_type_concept_id, "
                    "condition_status_source_value, condition_source_value, "
                    "condition_source_concept_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        next_id("condition"),
                        number,
                        0 if icd else source,  # an ICD-10 source maps to a standard concept
                        onset.isoformat() if onset else None,
                        EHR,
                        item.get("status"),
                        code if icd else None,
                        source,
                    ),
                )
            for item in _items(record.get("observations")):
                code = str(item.get("code") or "")
                measured = concept("Measurement", "LOINC", code, str(item.get("name") or ""))
                when = _date(item.get("date"))
                value = item.get("value")
                s.all(
                    f"INSERT INTO {t}measurement (measurement_id, person_id, "
                    "measurement_concept_id, measurement_date, measurement_type_concept_id, "
                    "value_as_number, unit_source_value, measurement_source_value) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        next_id("measurement"),
                        number,
                        measured,
                        when.isoformat() if when else None,
                        EHR,
                        value if isinstance(value, int | float) else None,
                        item.get("unit"),
                        code,
                    ),
                )
            for item in _items(record.get("medications")):
                rxnorm = str(item.get("code") or "")
                name = str(item.get("name") or "")
                drug = concept("Drug", "RxNorm", rxnorm, name) if rxnorm else 0
                start, end = _date(item.get("start")), _date(item.get("end"))
                status = item.get("status")
                s.all(
                    f"INSERT INTO {t}drug_exposure (drug_exposure_id, person_id, drug_concept_id, "
                    "drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, "
                    "stop_reason, drug_source_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        next_id("drug"),
                        number,
                        drug,
                        start.isoformat() if start else None,
                        end.isoformat() if end else None,
                        EHR,
                        status if status in ("completed", "stopped") else None,
                        item.get("name"),
                    ),
                )
            notes = record.get("notes")
            if isinstance(notes, str) and notes:
                s.all(
                    f"INSERT INTO {t}note (note_id, person_id, note_date, note_type_concept_id, "
                    "note_class_concept_id, note_title, note_text, encoding_concept_id, "
                    "language_concept_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (number, number, as_of.isoformat(), EHR, 0, "Clinic notes", notes, 0, 0),
                )
    return len(records)


def _prefix(schema: str) -> str:
    if not schema:
        return ""
    if not schema.isidentifier():
        raise OpenHealthError(f"{schema!r} is not a schema name")
    return f"{schema}."


def _grouped(session: Session, sql: str, ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    rows = session.all(sql.replace("{ids}", ", ".join("?" for _ in ids)), ids)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["person_id"]].append(row)
    return grouped


def _ordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: row["row_id"])


def _note_order(row: Mapping[str, Any]) -> tuple[str, int]:
    return (str(row["note_date"]), int(row["row_id"]))


def _day(value: Any) -> str | None:
    parsed = _date(str(value)) if value is not None else None
    return parsed.isoformat() if parsed else None


def _patient(person: Mapping[str, Any], as_of: date) -> dict[str, Any]:
    patient: dict[str, Any] = {}
    sex = GENDER.get(int(person["gender_concept_id"] or 0))
    if sex:
        patient["sex"] = sex
    year = int(person["year_of_birth"] or 0)
    if year:
        birth = date(year, int(person["month_of_birth"] or 7), int(person["day_of_birth"] or 1))
        patient["age"] = (
            as_of.year - birth.year - ((as_of.month, as_of.day) < (birth.month, birth.day))
        )
    return patient


def _condition(row: Mapping[str, Any]) -> dict[str, Any]:
    code = row["condition_source_value"] or row["source_code"] or row["code"]
    item: dict[str, Any] = {"code": str(code)}
    name = row["source_name"] or row["name"]
    if name:
        item["text"] = str(name)
    if row["condition_status_source_value"]:
        item["status"] = str(row["condition_status_source_value"])
    onset = _day(row["condition_start_date"])
    if onset:
        item["onset"] = onset
    return item


def _measurement(row: Mapping[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"code": str(row["code"] or row["measurement_source_value"])}
    if row["name"]:
        item["name"] = str(row["name"])
    if row["value_as_number"] is not None:
        item["value"] = float(row["value_as_number"])
    if row["unit_source_value"]:
        item["unit"] = str(row["unit_source_value"])
    when = _day(row["measurement_date"])
    if when:
        item["date"] = when
    return item


def _drug(row: Mapping[str, Any], as_of: date) -> dict[str, Any]:
    end = _day(row["drug_exposure_end_date"])
    active = end is None or end > as_of.isoformat()
    item: dict[str, Any] = {}
    if row["code"] and row["vocabulary_id"] == "RxNorm":
        item["code"] = str(row["code"])
    if row["drug_source_value"]:
        item["name"] = str(row["drug_source_value"])
    item["status"] = "active" if active else (row["stop_reason"] or "completed")
    start = _day(row["drug_exposure_start_date"])
    if start:
        item["start"] = start
    if end:
        item["end"] = end
    return item


def population(
    decisions: Sequence[Mapping[str, Any]], questions: Sequence[str], *, min_cell: int = 10
) -> dict[str, Any]:
    """Metrics for a decided population: the outcome mix and each question's label mix. Cells
    under `min_cell` people are suppressed (null), since a tiny cell can point at a person."""

    def cells(counter: Mapping[str, int]) -> dict[str, int | None]:
        return {k: (v if v >= min_cell else None) for k, v in sorted(counter.items())}

    outcomes: dict[str, int] = defaultdict(int)
    labels: dict[str, dict[str, int]] = {q: defaultdict(int) for q in questions}
    for decision in decisions:
        outcomes[str(decision["outcome"])] += 1
        for question in questions:
            answer = decision["answers"].get(question)
            labels[question][answer["label"] if answer else "none"] += 1
    return {
        "people": len(decisions),
        "min_cell": min_cell,
        "outcomes": cells(outcomes),
        "answers": {q: cells(counter) for q, counter in labels.items()},
    }
