from __future__ import annotations
from datetime import date
from typing import Literal
from pydantic import BaseModel


class ExtractedClaim(BaseModel):
    subject_mention: str          # exact surface form from the text
    attribute_key: str            # must exist in attribute_def
    value: str | float | None
    unit: str | None = None
    as_of: date | None = None     # when the source says it was true
    quote: str                    # verbatim substring of the document — verified mechanically
    char_start: int
    char_end: int
    extractor_confidence: Literal["high", "medium", "low"]


class ExtractionResult(BaseModel):
    claims: list[ExtractedClaim]
