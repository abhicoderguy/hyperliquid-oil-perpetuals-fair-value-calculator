import unittest
from zoneinfo import ZoneInfo

from fair_value_spline import WTIRollScheduleBuilder


ET = ZoneInfo("America/New_York")


def schedule_labels(year: int, month: int) -> list[str]:
    window = WTIRollScheduleBuilder("CL").build_roll_window(year, month)
    return [
        f"{shift.datetime_utc.astimezone(ET):%Y-%m-%d %H:%M %Z} "
        f"{shift.front_weight:.0%}/{shift.back_weight:.0%}"
        for shift in window.shifts
    ]


class WTIRollScheduleTest(unittest.TestCase):
    def test_april_2026_schedule_skips_good_friday(self) -> None:
        self.assertEqual(
            schedule_labels(2026, 4),
            [
                "2026-04-08 17:30 EDT 80%/20%",
                "2026-04-09 17:30 EDT 60%/40%",
                "2026-04-10 17:30 EDT 40%/60%",
                "2026-04-13 17:30 EDT 20%/80%",
                "2026-04-14 17:30 EDT 0%/100%",
            ],
        )

    def test_may_2026_schedule_matches_current_cloil_reference(self) -> None:
        self.assertEqual(
            schedule_labels(2026, 5),
            [
                "2026-05-07 17:30 EDT 80%/20%",
                "2026-05-08 17:30 EDT 60%/40%",
                "2026-05-11 17:30 EDT 40%/60%",
                "2026-05-12 17:30 EDT 20%/80%",
                "2026-05-13 17:30 EDT 0%/100%",
            ],
        )


if __name__ == "__main__":
    unittest.main()
