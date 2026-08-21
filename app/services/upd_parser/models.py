from datetime import date
from decimal import Decimal
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class UPDProduct:
    line_number: int
    raw_line_number: str | None
    raw_name: str
    name: str
    product_code: str | None
    unit: str | None
    okei: str | None
    gtin: str | None
    quantity: Decimal | None
    price: Decimal | None
    amount_without_vat: Decimal | None
    vat_rate: str | None
    vat_amount: Decimal | None
    amount_with_vat: Decimal | None
    series: str | None
    expiration_date: date | None


@dataclass(slots=True, frozen=True)
class UPDDocument:
    document_number: str | None
    document_data: date | None
    products: tuple[UPDProduct, ...]
