from datetime import date
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field


class RegistryProduct(BaseModel):
    model_config = ConfigDict(extra='forbid')

    line_number: int | None = None
    name: str | None = None
    product_code: str | None = None
    series: str | None = None
    manufacturer: str | None = None
    quantity: Decimal | None = None
    expiration_date: str | None = None


class RegistryDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')

    source_layout: str
    layout_confidence: float | None = None
    layout_source: str | None = None
    supplier: str | None = None
    buyer: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    products: list[RegistryProduct] = Field(default_factory=list)
