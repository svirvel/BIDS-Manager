"""Schema-driven validators (architecture.md §6).

Three scopes:

* ``validate_entity_set`` — given a (datatype, suffix) pair and a dict of
  entities, return one verdict per failing rule. Used by the GUI form to
  highlight missing/invalid fields in real time.
* ``validate_basename`` — parse a basename into (entities, suffix, ext) and
  run the same checks plus filename-order checks.
* ``validate_dataset`` — placeholder for full on-disk validation (deferred
  to ``ancpbids`` integration in a later milestone).

All three return ``list[ValidationVerdict]``. An empty list means "all OK".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

from . import engine
from .types import Datatype, Scope, Severity, Suffix, ValidationVerdict


def validate_entity_set(
    entities: Mapping[str, str],
    datatype: Datatype,
    suffix: Suffix,
) -> list[ValidationVerdict]:
    """Validate a set of entities against the schema for ``(datatype, suffix)``."""

    verdicts: list[ValidationVerdict] = []

    if datatype not in engine.list_datatypes():
        verdicts.append(
            ValidationVerdict(
                severity=Severity.ERROR,
                scope=Scope.ENTITY,
                rule_id="datatype.unknown",
                message=f"Unknown datatype: {datatype!r}",
            )
        )
        return verdicts

    if suffix not in engine.list_suffixes(datatype):
        verdicts.append(
            ValidationVerdict(
                severity=Severity.ERROR,
                scope=Scope.ENTITY,
                rule_id="suffix.unknown",
                message=f"Suffix {suffix!r} not valid for datatype {datatype!r}",
            )
        )
        return verdicts

    allowed = set(engine.allowed_entities(datatype, suffix))
    required = set(engine.required_entities(datatype, suffix))

    # Required entities present.
    for ent in required:
        if not entities.get(ent):
            verdicts.append(
                ValidationVerdict(
                    severity=Severity.ERROR,
                    scope=Scope.ENTITY,
                    rule_id="entity.required",
                    message=f"Required entity {ent!r} missing",
                )
            )

    # Provided entities are allowed and well-formed.
    for ent, value in entities.items():
        if value in (None, ""):
            continue
        if ent not in allowed:
            verdicts.append(
                ValidationVerdict(
                    severity=Severity.ERROR,
                    scope=Scope.ENTITY,
                    rule_id="entity.not_allowed",
                    message=f"Entity {ent!r} not allowed for {datatype}/{suffix}",
                )
            )
            continue
        try:
            fmt = engine.entity_format(ent)
        except KeyError:
            verdicts.append(
                ValidationVerdict(
                    severity=Severity.ERROR,
                    scope=Scope.ENTITY,
                    rule_id="entity.unknown",
                    message=f"Unknown entity: {ent!r}",
                )
            )
            continue
        if not re.fullmatch(fmt.pattern, str(value)):
            verdicts.append(
                ValidationVerdict(
                    severity=Severity.ERROR,
                    scope=Scope.ENTITY,
                    rule_id="entity.format",
                    message=(
                        f"Entity {ent!r} value {value!r} does not match "
                        f"{fmt.name} pattern /{fmt.pattern}/"
                    ),
                )
            )

    return verdicts


def validate_basename(
    basename: str,
    datatype: Datatype,
    extensions: tuple[str, ...] = engine.KNOWN_EXTENSIONS,
) -> list[ValidationVerdict]:
    """Parse + validate a BIDS basename. Strips extension; needs a known suffix."""

    name = basename
    for ext in extensions:
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    parts = name.split("_")
    if len(parts) < 2:
        return [
            ValidationVerdict(
                severity=Severity.ERROR,
                scope=Scope.BASENAME,
                rule_id="basename.no_suffix",
                message="Basename has no suffix component",
            )
        ]
    suffix = parts[-1]
    schema_entities, issues = engine._parse_entity_tokens(parts[:-1])

    verdicts: list[ValidationVerdict] = [
        ValidationVerdict(
            severity=Severity.ERROR, scope=Scope.BASENAME,
            rule_id=rule_id, message=message,
        )
        for rule_id, message in issues
    ]
    verdicts.extend(validate_entity_set(schema_entities, datatype, suffix))
    return verdicts


def validate_dataset(dataset_root: Path) -> list[ValidationVerdict]:
    """Stub — defer full validation to a later milestone (ancpbids reader)."""

    return [
        ValidationVerdict(
            severity=Severity.INFO,
            scope=Scope.DATASET,
            rule_id="dataset.not_yet_implemented",
            message=(
                "validate_dataset is a stub; full dataset validation lands in a "
                "later milestone (architecture.md §6, third bullet)."
            ),
        )
    ]


__all__ = ["validate_entity_set", "validate_basename", "validate_dataset"]
