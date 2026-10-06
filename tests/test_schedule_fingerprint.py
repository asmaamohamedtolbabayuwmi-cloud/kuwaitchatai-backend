import hashlib
import unittest

from app.services.schedule_fingerprint import (
    InvalidFingerprintData,
    build_schedule_identity,
)
from app.services.schedule_models import FingerprintCourseData, FingerprintData


class ScheduleFingerprintTests(unittest.TestCase):
    def test_normalizes_sorts_and_hashes_on_the_server(self):
        data = FingerprintData(
            studentId="٢٢١١١٣٤٧٣٣",
            academicYear="٢٠٢٥ / ٢٠٢٦",
            semester="الأول",
            courses=[
                FingerprintCourseData(
                    courseCode=" 1380332 ", section=" 01 ", status=" مسجل "
                ),
                FingerprintCourseData(
                    courseCode="0360311", section="1x04", status="منسحب"
                ),
                FingerprintCourseData(
                    courseCode="1360105", section="e1ax12", status="مسجل"
                ),
            ],
        )

        identity = build_schedule_identity(data)

        expected = (
            "2211134733|2025/2026|الأول|\n"
            "0360311|1X04|منسحب|\n"
            "1360105|E1AX12|مسجل|\n"
            "1380332|01|مسجل"
        )
        self.assertEqual(identity.canonical_string, expected)
        self.assertEqual(
            identity.fingerprint,
            hashlib.sha256(expected.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(identity.term_key, "2211134733_2025_2026_first")

    def test_input_order_and_exact_duplicate_rows_do_not_change_hash(self):
        first = FingerprintCourseData(courseCode="2", section="01", status="مسجل")
        second = FingerprintCourseData(courseCode="1", section="02", status="مسجل")
        base = dict(
            studentId="1",
            academicYear="2025/2026",
            semester="الثاني",
        )

        left = build_schedule_identity(FingerprintData(**base, courses=[first, second]))
        right = build_schedule_identity(
            FingerprintData(**base, courses=[second, first, first])
        )

        self.assertEqual(left.fingerprint, right.fingerprint)
        self.assertEqual(left.term_key, "1_2025_2026_second")

    def test_status_change_creates_a_new_version_in_the_same_term(self):
        base = dict(
            studentId="1",
            academicYear="2025/2026",
            semester="الصيفي",
        )
        registered = build_schedule_identity(
            FingerprintData(
                **base,
                courses=[
                    FingerprintCourseData(
                        courseCode="101", section="1", status="مسجل"
                    )
                ],
            )
        )
        withdrawn = build_schedule_identity(
            FingerprintData(
                **base,
                courses=[
                    FingerprintCourseData(
                        courseCode="101", section="1", status="منسحب"
                    )
                ],
            )
        )

        self.assertNotEqual(registered.fingerprint, withdrawn.fingerprint)
        self.assertEqual(registered.term_key, withdrawn.term_key)

    def test_rejects_incomplete_identity(self):
        with self.assertRaises(InvalidFingerprintData):
            build_schedule_identity(
                FingerprintData(
                    studentId="",
                    academicYear="2025/2026",
                    semester="الأول",
                    courses=[],
                )
            )


if __name__ == "__main__":
    unittest.main()
