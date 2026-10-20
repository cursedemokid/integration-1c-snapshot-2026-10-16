from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from app.document_matching import parse_document


@dataclass(frozen=True)
class Warehouse:
    code: str
    name: str
    group: str = ""
    accounting_name: str = ""


@dataclass(frozen=True)
class LedgerRow:
    period: str
    document: str
    nomenclature: str
    opening_quantity: Decimal
    opening_amount: Decimal
    turnover_quantity: Decimal
    turnover_amount: Decimal
    closing_quantity: Decimal
    closing_amount: Decimal
    nomenclature_code: str = ""
    article: str = ""
    warehouse: str = ""
    warehouse_code: str = ""
    warehouse_mapping_id: str = ""
    organization: str = ""
    organization_inn: str = ""
    sender_warehouse: str = ""
    receiver_warehouse: str = ""
    sender_warehouse_code: str = ""
    receiver_warehouse_code: str = ""
    sender_accounting_warehouse: str = ""
    receiver_accounting_warehouse: str = ""
    transfer_scope: str = ""
    document_id: str = ""
    source_document_id: str = ""
    party: str = ""
    counterparty: str = ""
    is_import: str = ""
    related_documents: tuple[str, ...] = ()
    issued_invoice_number: str = ""
    received_invoice_number: str = ""
    supplier_order: str = ""
    unit: str = ""
    package: str = ""
    package_coefficient: str = ""
    party_comment: str = ""
    party_waybill: str = ""
    document_type: str = ""
    document_number: str = ""
    document_date: str = ""
    present_fields: frozenset[str] = frozenset()
    aggregation_conflict: bool = False
    warehouse_mapping_error: str = ""
    balance_point: str = ""
    is_aggregated: bool = False

    @property
    def match_key(self) -> tuple[str, ...]:
        document = parse_document(self.document, self.period)
        return (
            document.timestamp,
            document.family,
            document.number.casefold(),
            "code" if self.nomenclature_code else "name",
            self.nomenclature_code or self.nomenclature,
            "mapping" if self.warehouse_mapping_id else ("code" if self.warehouse_code else "name"),
            self.warehouse_mapping_id or self.warehouse_code or self.warehouse,
            "inn" if self.organization_inn else ("name" if self.organization else "none"),
            (self.organization_inn or self.organization).casefold(),
        )


@dataclass(frozen=True)
class SourceObservation:
    """Values from one original row; observing a boundary is not comparison approval."""

    period: str
    opening_quantity: Decimal
    closing_quantity: Decimal
    observed_point: str = ""
    control_date: date | None = None


@dataclass(frozen=True)
class Discrepancy:
    period: str
    base: str
    document: str
    nomenclature: str
    opening_quantity: Decimal
    opening_amount: Decimal
    turnover_quantity: Decimal
    turnover_amount: Decimal
    closing_quantity: Decimal
    closing_amount: Decimal
    discrepancy_type: str
    recommendation: str
    nomenclature_code: str = ""
    article: str = ""
    warehouse: str = ""
    warehouse_code: str = ""
    warehouse_mapping_id: str = ""
    organization: str = ""
    organization_inn: str = ""
    trade_row: LedgerRow | None = None
    tax_row: LedgerRow | None = None
    match_method: str = ""
    difference_summary: str = ""
    document_existence: Literal["found", "confirmed_absent", "unknown"] = "unknown"
    trade_document_refs: tuple[str, ...] = ()
    tax_document_refs: tuple[str, ...] = ()
    balance_point: str = ""
    balance_control_date: date | None = None
    balance_trade_quantity: Decimal | None = None
    balance_tax_quantity: Decimal | None = None
    trade_source: SourceObservation | None = None
    tax_source: SourceObservation | None = None


@dataclass(frozen=True)
class ReconciliationSummary:
    base: str
    trade_documents: int = 0
    tax_documents: int = 0
    matched_documents: int = 0
    matched_import_groups: int = 0
    matched_by_number_date: int = 0
    matched_by_content: int = 0
    ambiguous_documents: int = 0
    duplicate_documents: int = 0
    missing_documents: int = 0
    extra_documents: int = 0
    buh_only_documents: int = 0
    ignored_internal_transfers: int = 0
    excluded_trade_documents: int = 0
    excluded_tax_documents: int = 0
    pending_trade_documents: int = 0
    pending_tax_documents: int = 0
    document_quantity_differences: int = 0
    document_amount_differences: int = 0
    balance_quantity_differences: int = 0
    balance_amount_differences: int = 0
    trade_document_groups: int = 0
    tax_document_groups: int = 0
    matched_document_groups: int = 0
    trade_item_rows: int = 0
    tax_item_rows: int = 0
    unverified_documents: int = 0
    manual_item_rows: int = 0


@dataclass(frozen=True)
class ReconciliationNotice:
    """A source limitation, rather than an instruction to fix a document."""

    base: str
    reason: str
    affected_rows: int


@dataclass(frozen=True)
class ReconciliationResult:
    discrepancies: list[Discrepancy]
    summaries: list[ReconciliationSummary] | None = None
    source_errors: dict[str, str] = field(default_factory=dict)
    start_date: date | None = None
    end_date: date | None = None
    notices: list[ReconciliationNotice] = field(default_factory=list)
