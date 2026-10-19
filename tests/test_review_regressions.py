"""Input and operator failures found during the application-day review."""
import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

from bbp_demo.__main__ import main
from bbp_demo.data import FIELDS, generated_rows, load_dataset
from bbp_demo.report import independent_accounting


class ReviewRegressions(unittest.TestCase):
    def test_every_nonfinite_ohlc_field_is_rejected(self):
        # min/max can conceal NaN when it is the second operand.
        for field in ("open", "high", "low", "close"):
            for invalid in ("nan", "inf", "-inf"):
                with self.subTest(field=field, invalid=invalid), tempfile.TemporaryDirectory() as folder:
                    row = list(next(generated_rows()))
                    row[FIELDS.index(field)] = invalid
                    path = Path(folder) / "bad.csv"
                    with path.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.writer(handle)
                        writer.writerow(FIELDS)
                        writer.writerow(row)
                    with self.assertRaisesRegex(ValueError, "must be finite"):
                        load_dataset(path)

    def test_missing_report_is_an_operator_error_without_traceback(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as exit_result:
            main(["dashboard", "--report", "missing-report-for-regression.json"])
        self.assertEqual(exit_result.exception.code, 2)
        self.assertIn("error:", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_accounting_rejects_unknown_side_before_using_sign(self):
        result = {"trades": [{"side": "INVALID"}]}
        with self.assertRaisesRegex(ValueError, "LONG or SHORT"):
            independent_accounting(result, 100000, {})


if __name__ == "__main__":
    unittest.main()
