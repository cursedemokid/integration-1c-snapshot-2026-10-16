"""Transfer endpoint uncertainty must be scoped to the affected document."""

from unittest import TestCase

from app.normalization import normalize_rows
from app.reconciliation import AMBIGUOUS_MATCH, DOCUMENT_QUANTITY_MISMATCH, reconcile


def transfer(document, quantity, *, sender="W1", receiver="W2"):
    return {
        "Период": "02.07.2026 10:00:00",
        "Документ": document,
        "Номенклатура": "Товар 1",
        "НоменклатураКод": "ITEM-1",
        "Склад": "W1",
        "СкладКод": "W1",
        "Организация": "Тест",
        "ОрганизацияИНН": "111",
        "ЕдиницаИзмерения": "шт",
        "КоличествоНачальныйОстаток": 0,
        "СтоимостьНачальныйОстаток": 0,
        "КоличествоОборот": quantity,
        "СтоимостьОборот": 0,
        "КоличествоКонечныйОстаток": 0,
        "СтоимостьКонечныйОстаток": 0,
        "СкладОтправительКод": sender,
        "СкладПолучательКод": receiver,
    }


class TransferScopeTests(TestCase):
    def test_missing_endpoint_codes_quarantine_only_that_transfer_document(self):
        trade = normalize_rows([
            transfer("Перемещение товаров A1 от 02.07.2026 10:00:00", -5),
            transfer("Перемещение товаров B1 от 02.07.2026 10:00:00", -7, sender="", receiver="W2"),
        ], "")
        tax = normalize_rows([
            transfer("Перемещение товаров A1 от 02.07.2026 10:00:00", -6),
            transfer("Перемещение товаров B1 от 02.07.2026 10:00:00", -7, sender="", receiver="W2"),
        ], "")

        result = reconcile(trade, {"БУХ2026": tax}, "02.07.2026")

        quantities = [d for d in result.discrepancies
                      if d.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH]
        ambiguous = [d for d in result.discrepancies if d.discrepancy_type == AMBIGUOUS_MATCH]
        assert len(quantities) == 1
        assert quantities[0].trade_row.document.endswith("A1 от 02.07.2026 10:00:00")
        assert quantities[0].trade_row.turnover_quantity == -5
        assert quantities[0].tax_row.turnover_quantity == -6
        assert len(ambiguous) == 2
        assert {d.trade_row.document if d.trade_row else d.tax_row.document for d in ambiguous} == {
            "Перемещение товаров B1 от 02.07.2026 10:00:00",
        }
