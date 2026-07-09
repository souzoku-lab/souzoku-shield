from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field, field_validator


AcquirerType = Literal["spouse", "co_resident", "house_lost"]
PartitionStatus = Literal["in_progress", "expected", "finalized"]
DocumentStatus = Literal["not_requested", "requested", "received", "verified"]
HeirRelation = Literal["spouse", "child"]
HeirRelationship = Literal[
    "spouse",
    "eldest_son",
    "eldest_daughter",
    "second_son",
    "second_daughter",
    "third_son",
    "third_daughter",
]


class CasePatch(BaseModel):
    acquirer_type: AcquirerType | None = None
    home_acquirer_id: str | None = Field(default=None, max_length=60)
    partition_status: PartitionStatus | None = None


class HeirPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    relation: HeirRelation | None = None
    co_resident: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return unicodedata.normalize("NFKC", str(value)).strip()


class HeirCreateRequest(BaseModel):
    relationship: HeirRelationship
    co_resident: bool


class DocumentPatch(BaseModel):
    status: DocumentStatus


class ManualOpinionPatch(BaseModel):
    overall_opinion: str = Field(max_length=2000)

    @field_validator("overall_opinion")
    @classmethod
    def normalize_overall_opinion(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value))
        normalized = re.sub(r"[ \t]+\n", "\n", normalized)
        normalized = re.sub(r"\n{4,}", "\n\n\n", normalized).strip()
        return normalized


class HealthResponse(BaseModel):
    ok: bool
    service: str
    storage: str
    llm_required: bool
    gemini_configured: bool


class IntakeRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    acquirer_type: AcquirerType = "co_resident"
    partition_status: PartitionStatus = "in_progress"


class ConsultationRunRequest(BaseModel):
    text: str = Field()

    @field_validator("text")
    @classmethod
    def normalize_and_validate_text(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value))
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if len(normalized) < 8:
            raise ValueError("相談文は8文字以上で入力してください。")
        if len(normalized) > 1200:
            raise ValueError("相談文は1200文字以内で入力してください。")
        return normalized
