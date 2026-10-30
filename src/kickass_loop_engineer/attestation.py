"""SLSA Level 3 / In-Toto v1 build attestation creation and verification."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Optional

IN_TOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
HIVEMIND_PREDICATE_TYPE = "https://hivemind.ai/attestation/v1"


@dataclass
class AttestationReceipt:
    """Structured receipt representing an In-Toto / SLSA build attestation."""

    subject_name: str
    candidate_digest: str
    builder_id: str
    reviewer_id: str
    proof_passed: bool
    confined: bool
    statement_digest: str = ""
    signature: Optional[str] = None
    statement: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the receipt."""
        return {
            "subject_name": self.subject_name,
            "candidate_digest": self.candidate_digest,
            "builder_id": self.builder_id,
            "reviewer_id": self.reviewer_id,
            "proof_passed": self.proof_passed,
            "confined": self.confined,
            "statement_digest": self.statement_digest,
            "signature": self.signature,
            "statement": dict(self.statement),
        }

    @classmethod
    def from_statement(cls, statement: dict[str, Any]) -> "AttestationReceipt":
        """Construct an :class:`AttestationReceipt` from a statement dictionary."""
        subjects = statement.get("subject") or [{}]
        first_subject = subjects[0] if subjects else {}
        predicate = statement.get("predicate") or {}
        digest_map = first_subject.get("digest") or {}
        return cls(
            subject_name=str(first_subject.get("name", "")),
            candidate_digest=str(predicate.get("candidate_digest") or digest_map.get("sha256", "")),
            builder_id=str(predicate.get("builder_id") or (predicate.get("builder") or {}).get("id", "")),
            reviewer_id=str(predicate.get("reviewer_id") or (predicate.get("reviewer") or {}).get("id", "")),
            proof_passed=bool(predicate.get("proof_passed", False)),
            confined=bool(predicate.get("confined", False)),
            statement_digest=str(statement.get("statement_digest", "")),
            signature=statement.get("signature"),
            statement=dict(statement),
        )


def _canonical_statement_bytes(statement: dict[str, Any]) -> bytes:
    """Serialize the core In-Toto statement fields deterministically for hashing and signing."""
    core = {
        "_type": statement.get("_type"),
        "subject": statement.get("subject"),
        "predicateType": statement.get("predicateType"),
        "predicate": statement.get("predicate"),
    }
    return json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")


def create_attestation(
    subject_name: str,
    candidate_digest: str,
    builder_id: str,
    reviewer_id: str,
    proof_passed: bool,
    confined: bool,
    signing_key: Optional[str] = None,
) -> dict[str, Any]:
    """Create an In-Toto v1 build attestation statement with SHA-256 digest and optional HMAC-SHA256 signature."""
    clean_sha = (
        candidate_digest.split(":", 1)[1]
        if candidate_digest.startswith("sha256:")
        else candidate_digest
    )
    statement: dict[str, Any] = {
        "_type": IN_TOTO_STATEMENT_TYPE,
        "subject": [
            {
                "name": subject_name,
                "digest": {"sha256": clean_sha},
            }
        ],
        "predicateType": HIVEMIND_PREDICATE_TYPE,
        "predicate": {
            "builder": {"id": builder_id},
            "builder_id": builder_id,
            "reviewer": {"id": reviewer_id},
            "reviewer_id": reviewer_id,
            "candidate_digest": candidate_digest,
            "proof_passed": bool(proof_passed),
            "confined": bool(confined),
            "slsa_level": 3 if (proof_passed and confined) else 1,
        },
    }
    canonical = _canonical_statement_bytes(statement)
    statement["statement_digest"] = hashlib.sha256(canonical).hexdigest()
    if signing_key is not None:
        statement["signature"] = hmac.new(
            signing_key.encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).hexdigest()
    else:
        statement["signature"] = None
    return statement


def verify_attestation(
    statement: dict[str, Any],
    signing_key: Optional[str] = None,
) -> bool:
    """Verify an In-Toto v1 attestation's structural integrity, digest, and optional HMAC-SHA256 signature."""
    if not isinstance(statement, dict):
        return False
    if statement.get("_type") != IN_TOTO_STATEMENT_TYPE:
        return False
    if statement.get("predicateType") != HIVEMIND_PREDICATE_TYPE:
        return False
    subjects = statement.get("subject")
    predicate = statement.get("predicate")
    if not isinstance(subjects, list) or not subjects or not isinstance(predicate, dict):
        return False
    first_subj = subjects[0]
    if not isinstance(first_subj, dict) or not first_subj.get("name"):
        return False
    digest_obj = first_subj.get("digest")
    if not isinstance(digest_obj, dict) or not digest_obj.get("sha256"):
        return False

    canonical = _canonical_statement_bytes(statement)
    expected_digest = hashlib.sha256(canonical).hexdigest()
    actual_digest = statement.get("statement_digest")
    if not isinstance(actual_digest, str) or not hmac.compare_digest(actual_digest, expected_digest):
        return False

    actual_signature = statement.get("signature")
    if signing_key is not None:
        if not isinstance(actual_signature, str) or not actual_signature:
            return False
        expected_signature = hmac.new(
            signing_key.encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(actual_signature, expected_signature):
            return False
    else:
        if actual_signature:
            return False

    return True
