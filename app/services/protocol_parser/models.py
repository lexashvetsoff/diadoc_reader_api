from datetime import date
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field


class ProtocolProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: int | None = None
    name: str | None = None
    product_code: str | None = None
    gtin: str | None = None
    series: str | None = None
    manufacturer: str | None = None
    manufacturer_actual_price_without_vat: Decimal | None = None
    manufacturer_regulated_price: Decimal | None = None
    quantity: Decimal | None = None
    wholesale_price: Decimal | None = None
    manufacturer_sale_date: date | None = None
    expiration_date: str | None = None


class ProtocolDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_number: str | None = None
    document_date: date | None = None
    supplier: str | None = None
    buyer: str | None = None
    basis_number: str | None = None
    basis_date: date | None = None
    source_format: str
    products: list[ProtocolProduct] = Field(default_factory=list)
