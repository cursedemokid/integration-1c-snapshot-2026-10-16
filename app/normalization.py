from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.models import LedgerRow


class NormalizationError(ValueError):
    pass


FIELD_ALIASES = {
    "period": ("period", "Период", "период"),
    "document": ("document", "Документ", "документ"),
    "nomenclature": ("nomenclature", "Номенклатура", "номенклатура"),
    "opening_quantity": ("opening_quantity", "КоличествоНачальныйОстаток", "Количество начальный остаток"),
    "opening_amount": ("opening_amount", "СтоимостьНачальныйОстаток", "Стоимость начальный остаток", "СуммаНачальныйОстаток"),
    "turnover_quantity": ("turnover_quantity", "КоличествоОборот", "Количество оборот"),
    "turnover_amount": ("turnover_amount", "СтоимостьОборот", "Стоимость оборот", "СуммаОборот"),
    "closing_quantity": ("closing_quantity", "КоличествоКонечныйОстаток", "Количество конечный остаток"),
    "closing_amount": ("closing_amount", "СтоимостьКонечныйОстаток", "Стоимость конечный остаток", "СуммаКонечныйОстаток"),
    "nomenclature_code": ("nomenclature_code", "НоменклатураКод"),
    "article": ("article", "НоменклатураАртикул"),
    "warehouse": ("warehouse", "Склад", "СкладПредставление"),
    "warehouse_code": ("warehouse_code", "СкладКод"),
    "organization": ("organization", "Организация"),
    "organization_inn": ("organization_inn", "ОрганизацияИНН"),
    "sender_warehouse": ("sender_warehouse", "СкладОтправитель"),
    "receiver_warehouse": ("receiver_warehouse", "СкладПолучатель"),
    "sender_warehouse_code": ("sender_warehouse_code", "СкладОтправительКод"),
    "receiver_warehouse_code": ("receiver_warehouse_code", "СкладПолучательКод"),
    "document_id": ("document_id", "ДокументGUID", "ДокументУИД", "ДокументUUID"),
    "source_document_id": (
        "source_document_id",
        "ИсходныйДокументGUID",
        "ДокументОснованиеGUID",
        "ДокументОснованиеУИД",
    ),
    "party": ("party", "ПартияБух"),
    "counterparty": ("counterparty", "КонтрагентДляСверки", "НаименованиеОтчетКонтрагента", "НаименованиеДляДокументов"),
    "is_import": ("is_import", "ЭтоИмпорт"),
    "issued_invoice_number": ("issued_invoice_number", "НомерСФВыданный"),
    "received_invoice_number": ("received_invoice_number", "НомерСФПолученный"),
    "supplier_order": ("supplier_order", "Заказ", "ЗаказПоставщику"),
    "unit": ("unit", "ЕдиницаИзмерения"),
    "package": ("package", "Упаковка"),
    "package_coefficient": ("package_coefficient", "КоэффициентПересчета", "КоэффициентУпаковки"),
    "party_comment": ("party_comment", "Комментарий"),
    "party_waybill": ("party_waybill", "НакладнаяУПД", "НомерНакладнойУПД"),
    "document_type": ("document_type", "ВидДокумента"),
    "document_number": ("document_number", "НомерДокумента"),
    "document_date": ("document_date", "ДатаДокумента"),
}


def normalize_rows(payload: Any, default_period: str) -> list[LedgerRow]:
    records = _extract_records(payload)
    return [normalize_row(record, default_period) for record in records]


def normalize_row(record: dict[str, Any], default_period: str) -> LedgerRow:
    if not isinstance(record, dict):
        raise NormalizationError("Строка выгрузки должна быть объектом")

    period = _normalize_text(_get_value(record, "period") or default_period)
    document = _normalize_text(_get_value(record, "document") or "")
    nomenclature = _normalize_text(_get_value(record, "nomenclature") or "")

    if not period:
        raise NormalizationError("Период не должен быть пустым")

    return LedgerRow(
        period=period,
        document=document,
        nomenclature=nomenclature,
        opening_quantity=_normalize_amount(_required_value(record, "opening_quantity")),
        opening_amount=_normalize_amount(_required_value(record, "opening_amount")),
        turnover_quantity=_normalize_amount(_required_value(record, "turnover_quantity")),
        turnover_amount=_normalize_amount(_required_value(record, "turnover_amount")),
        closing_quantity=_normalize_amount(_required_value(record, "closing_quantity")),
        closing_amount=_normalize_amount(_required_value(record, "closing_amount")),
        **{name: _normalize_text(_optional_value(record, name)) for name in (
            "nomenclature_code", "article", "warehouse", "warehouse_code", "organization", "organization_inn",
            "sender_warehouse", "receiver_warehouse", "document_id", "source_document_id",
            "sender_warehouse_code", "receiver_warehouse_code",
            "party", "counterparty", "is_import", "issued_invoice_number",
            "received_invoice_number", "supplier_order", "unit", "package",
            "package_coefficient", "party_comment", "party_waybill",
            "document_type", "document_number", "document_date",
        )},
        present_fields=frozenset(
            name for name in FIELD_ALIASES if _has_field(record, name)
        ),
    )


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if "code" in payload and str(payload["code"]) != "200":
            raise NormalizationError(f"1С вернула код ошибки {payload['code']}")
        for key in ("data", "items", "rows", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise NormalizationError("Ответ 1С должен быть списком строк или объектом с data/items/rows/result")


def _required_value(record: dict[str, Any], canonical_name: str) -> Any:
    value = _get_value(record, canonical_name)
    if value is None:
        raise NormalizationError(f"Не найдено обязательное поле {canonical_name}")
    return value


def _get_value(record: dict[str, Any], canonical_name: str) -> Any:
    aliases = FIELD_ALIASES[canonical_name]
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for alias in aliases:
        direct = record.get(alias)
        if direct is not None:
            return direct
        lowered_value = lowered.get(alias.lower())
        if lowered_value is not None:
            return lowered_value
    return None


def _has_field(record: dict[str, Any], canonical_name: str) -> bool:
    keys = {str(key).strip().casefold() for key in record}
    return any(alias.casefold() in keys for alias in FIELD_ALIASES[canonical_name])


def _optional_value(record: dict[str, Any], canonical_name: str) -> Any:
    value = _get_value(record, canonical_name)
    return "" if value is None else value


def _normalize_text(value: Any) -> str:
    return " ".join(str(value).strip().split())


def _normalize_amount(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        amount = value
    else:
        prepared = (
            str(value)
            .strip()
            .replace(" ", "")
            .replace("\u00a0", "")
            .replace("\u202f", "")
            .replace(",", ".")
        )
        try:
            amount = Decimal(prepared)
        except InvalidOperation as exc:
            raise NormalizationError(f"Некорректный формат числа: {value}") from exc
    if not amount.is_finite():
        raise NormalizationError(f"Число должно быть конечным: {value}")
    return amount
