from datetime import date
from typing import Literal
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field


class UnifiedProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: int
    name: str
    product_code: str | None = None
    unit: str | None = None
    okei: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    amount_without_vat: Decimal | None = None
    vat_rate: str | None = None
    vat_amount: Decimal | None = None
    amount_with_vat: Decimal | None = None
    gtin: str | None = None
    series: str | None = None
    manufacturer: str | None = None
    manufacturer_actual_price_without_vat: Decimal | None = None
    manufacturer_regulated_price: Decimal | None = None
    wholesale_price: Decimal | None = None
    expiration_date: str | None = None
    manufacturer_sale_date: date | None = None


class UnifiedDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')

    upd_number: str | None = None
    upd_date: date | None = None
    products: list[UnifiedProduct] = Field(default_factory=list)


class MatchDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')

    source: Literal['registry', 'protocol']
    upd_line_number: int
    status: Literal['matched', 'unmatched', 'ambiguous']
    source_line_number: int | None = None
    score: float | None = None
    method: str | None = None
    evidence: list[str] = Field(default_factory=list)


class MergeWarning(BaseModel):
    model_config = ConfigDict(extra='forbid')

    code: str
    upd_line_number: int | None = None
    field: str | None = None
    message: str


class MergeReport(BaseModel):
    model_config = ConfigDict(extra='forbid')

    registry_matches: list[MatchDecision] = Field(default_factory=list)
    protocol_matches: list[MatchDecision] = Field(default_factory=list)
    unmatched_registry_lines: list[int] = Field(default_factory=list)
    unmatched_protocol_lines: list[int] = Field(default_factory=list)
    warnings: list[MergeWarning] = Field(default_factory=list)


class MergeResult(BaseModel):
    model_config = ConfigDict(extra='forbid')

    document: UnifiedDocument
    report: MergeReport
