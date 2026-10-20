from unittest import TestCase

from app.document_matching import (
    document_reconciliation_status,
    is_reconcilable_document,
    is_requested_document,
)


class RequestedDocumentScopeTests(TestCase):
    def test_requested_document_scope_includes_requested_types(self):
        documents = [
            "Комплектация номенклатуры КМ00-000001 от 01.01.2026 10:00:00",
            "Перемещение товаров ТР00-000001 от 01.01.2026 10:00:00",
            "Передача товаров филиалу ТР00-000001 от 01.01.2026 10:00:00",
            "Требование накладная РН00-000001 от 01.01.2026 10:00:00",
            "Требование-накладная РН00-000001 от 01.01.2026 10:00:00",
            "Безвозмездная передача БП00-000001 от 01.01.2026 10:00:00",
            "Оприходование ОП00-000001 от 01.01.2026 10:00:00",
            "Поступление товаров ПТ00-000001 от 01.01.2026 10:00:00",
            "Реализация товаров и услуг РТ00-000001 от 01.01.2026 10:00:00",
        ]
        self.assertTrue(all(is_requested_document(document) for document in documents))

    def test_requested_document_scope_rejects_other_known_and_unknown_types(self):
        self.assertFalse(is_requested_document(
            "Поступление дополнительных расходов ДР00-000001 от 01.01.2026 10:00:00"
        ))
        self.assertFalse(is_requested_document(
            "Неизвестный документ ХХ00-000001 от 01.01.2026 10:00:00"
        ))
        self.assertFalse(is_requested_document("Произвольный непарсируемый документ"))

    def test_requested_scope_keeps_unparsed_document_with_explicit_requested_type(self):
        self.assertTrue(is_requested_document("Комплектация номенклатуры без стандартного номера"))
        self.assertTrue(is_requested_document("Требование накладная без стандартного номера"))
        self.assertFalse(is_requested_document("Поступление дополнительных расходов без номера"))

    def test_requested_document_type_can_be_supplied_when_document_text_is_unparsed(self):
        self.assertTrue(is_requested_document("строка без формата", document_type="Реализация"))
        self.assertFalse(is_requested_document("строка без формата", document_type="Неизвестный тип"))

    def test_assembly_requirement_and_free_transfer_are_reconcilable(self):
        documents = [
            "Комплектация номенклатуры КМ00-000001 от 01.01.2026 10:00:00",
            "Требование-накладная РН00-000001 от 01.01.2026 10:00:00",
            "Безвозмездная передача БП00-000001 от 01.01.2026 10:00:00",
        ]
        for document in documents:
            with self.subTest(document=document):
                self.assertTrue(is_reconcilable_document(document))
                self.assertEqual(document_reconciliation_status(document), "included")
