import unittest

from app.report_details import expand_manual_rows


class ExpandManualRowsTests(unittest.TestCase):
    def test_splits_aligned_details_and_repeats_metadata(self):
        headers = [
            "База", "Артикул", "Номенклатура", "Количество УТ", "Количество БУХ",
            "Разница БУХ − УТ", "Период исходной строки УТ",
            "Исходный начальный остаток УТ", "Исходный конечный остаток УТ",
            "Период исходной строки БУХ", "Исходный начальный остаток БУХ",
            "Исходный конечный остаток БУХ",
        ]
        row = {
            "База": "БУХ2019",
            "Артикул": "A1\nA2",
            "Номенклатура": "Товар 1\nТовар 2",
            "Количество УТ": "0\n2",
            "Количество БУХ": "\n3",
            "Разница БУХ − УТ": "\n1",
            "Период исходной строки УТ": "p1\np2",
            "Исходный начальный остаток УТ": "0\n",
            "Исходный конечный остаток УТ": "4\n5",
            "Период исходной строки БУХ": "\np2",
            "Исходный начальный остаток БУХ": "\n6",
            "Исходный конечный остаток БУХ": "7\n8",
        }

        expanded = expand_manual_rows(headers, [row])

        self.assertEqual(len(expanded), 2)
        self.assertEqual([item["Артикул"] for item in expanded], ["A1", "A2"])
        self.assertEqual([item["Номенклатура"] for item in expanded], ["Товар 1", "Товар 2"])
        self.assertEqual([item["Количество УТ"] for item in expanded], ["0", "2"])
        self.assertEqual([item["Количество БУХ"] for item in expanded], ["", "3"])
        self.assertEqual([item["Исходный начальный остаток УТ"] for item in expanded], ["0", ""])
        self.assertEqual([item["База"] for item in expanded], ["БУХ2019", "БУХ2019"])

    def test_preserves_single_finding_and_true_zero(self):
        row = {"База": "БУХ2019", "Артикул": "", "Номенклатура": "Товар", "Количество УТ": "0"}

        self.assertEqual(expand_manual_rows(list(row), [row]), [row])

    def test_rejects_misaligned_multiline_detail(self):
        row = {"Номенклатура": "Товар 1\nТовар 2", "Количество УТ": "1"}

        with self.assertRaisesRegex(ValueError, "Количество УТ"):
            expand_manual_rows(list(row), [row])
