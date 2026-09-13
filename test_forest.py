"""The forest is only worth something if the comparison is fair, so most of
these tests guard the comparison rather than the model: no feature may see the
day it forecasts, the forest may never train on the month it is scored on, and
both sides are scored the same way."""

import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import testing
import evaluate
import forest
import store

TZ = ZoneInfo("America/Los_Angeles")

try:
    import numpy  # noqa: F401
    import sklearn  # noqa: F401
    MODELLING = True
except ImportError:
    MODELLING = False
needs_modelling = unittest.skipUnless(MODELLING, "needs requirements-ml.txt")


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


class FeatureTest(testing.DatabaseTest):
    TARGET = date(2026, 3, 16)   # a Monday in spring term

    def insert(self, stamped_counts):
        with self.connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO readings (observed_at, count, capacity) VALUES (%s, %s, 150)",
                stamped_counts)
        self.connection.commit()

    def day_of(self, day, count):
        """Six readings an hour from 8am to 8pm, all at `count`."""
        return [(at(day, hour, minute), count) for hour in range(8, 20) for minute in range(0, 60, 10)]

    def seed_five_weeks(self):
        rows = []
        for back in range(1, 36):
            day = self.TARGET - timedelta(days=back)
            rows += self.day_of(day, 40 + 10 * day.weekday())
        self.insert(rows)

    def features(self):
        return [row.features for row in forest.build_rows(self.connection, self.TARGET, self.TARGET)]

    def test_nothing_from_the_forecast_day_or_after_reaches_its_features(self):
        self.seed_five_weeks()
        before = self.features()
        self.insert(self.day_of(self.TARGET, 150)
                    + self.day_of(self.TARGET + timedelta(days=1), 150)
                    + self.day_of(self.TARGET + timedelta(days=7), 150))

        self.assertEqual(self.features(), before)

    def test_the_curve_feature_is_the_dashboards_curve_as_of_the_night_before(self):
        """Instances are compared as well as medians: a median shrugs off a
        couple of extra busy Mondays, which is the point of using one, so the
        median alone would not notice the curve had seen the future."""
        self.seed_five_weeks()
        self.insert(self.day_of(self.TARGET + timedelta(days=7), 150)
                    + self.day_of(self.TARGET + timedelta(days=14), 150))   # later Mondays
        midnight = datetime(self.TARGET.year, self.TARGET.month, self.TARGET.day, tzinfo=TZ)
        bands, weeks, _ = store.weekday_bands(self.connection, self.TARGET, str(TZ), forest.BUCKET_MINUTES,
                                              evaluate.WINDOW_INSTANCES, before=midnight)
        rows = forest.build_rows(self.connection, self.TARGET, self.TARGET)

        self.assertEqual(weeks, 5)
        self.assertEqual({r.weeks for r in rows}, {weeks})
        self.assertEqual({r.hour: r.curve for r in rows if r.curve is not None},
                         {hour: band["median"] for hour, band in bands.items()})

    def test_an_hour_needs_four_readings_to_count(self):
        self.insert([(at(self.TARGET, 9, minute), 60) for minute in (0, 10, 20)]
                    + [(at(self.TARGET, 10, minute), 60) for minute in (0, 10, 20, 30)])
        rows = {r.hour: r for r in forest.build_rows(self.connection, self.TARGET, self.TARGET)}

        self.assertIsNone(rows[9].actual)
        self.assertAlmostEqual(rows[10].actual, 60 / 150)


class CalendarFeatureTest(unittest.TestCase):
    """Position in the academic year, from the checked-in calendar."""

    @classmethod
    def setUpClass(cls):
        cls.periods = forest.calendar_periods()

    def named(self, day, kind):
        return dict(zip(forest.FEATURES[2:], forest.calendar_features(day, kind, self.periods)))

    def test_position_in_term_counts_from_the_semester_start(self):
        features = self.named(date(2026, 9, 14), "instruction")    # Fall 2026 began 19 August

        self.assertEqual(features["days_since_term_start"], 26)
        self.assertEqual(features["period_instruction"], 1.0)

    def test_a_break_inside_term_is_measured_from_the_break(self):
        features = self.named(date(2026, 11, 26), "holiday")        # Thanksgiving

        self.assertEqual(features["days_into_period"], 1, "the break began the day before")
        self.assertEqual(features["days_since_term_start"], 99)
        self.assertEqual(features["period_holiday"], 1.0)


class ScoreTest(unittest.TestCase):
    def forecasts(self, day, hours, curve, forest_value, actual=0.5):
        return [forest.Forecast(day, hour, "instruction", actual, curve, forest_value)
                for hour in range(hours)]

    def test_each_day_counts_once_however_many_hours_it_scored(self):
        days = forest.per_day(self.forecasts(date(2026, 1, 5), 24, curve=0.6, forest_value=0.5)
                              + self.forecasts(date(2026, 1, 6), 2, curve=1.0, forest_value=0.5))
        [overall] = [c for c in forest.compare(days) if c.kind == "all"]

        self.assertAlmostEqual(overall.curve_error, 0.3, msg="(0.1 + 0.5) / 2, not weighted by hours")
        self.assertEqual(overall.forest_better, 1.0)

    @needs_modelling
    def test_a_consistent_improvement_has_a_tight_interval(self):
        days = [forest.DayScore(date(2026, 1, 5) + timedelta(days=i), "instruction",
                                curve_error=0.10, forest_error=0.07, curve_bias=0.0, forest_bias=0.0)
                for i in range(70)]
        low, high = forest.weekly_bootstrap(days)

        self.assertAlmostEqual(low, -0.03)
        self.assertAlmostEqual(high, -0.03)


@needs_modelling
class WalkForwardTest(unittest.TestCase):
    def rows(self, start, end, actual, curve, features, weeks=lambda day: 8):
        rows, day = [], start
        while day <= end:
            for hour in range(24):
                rows.append(forest.Row(day=day, hour=hour, kind="instruction",
                                       features=features(day, hour), actual=actual(day, hour),
                                       curve=curve(day, hour), weeks=weeks(day)))
            day += timedelta(days=1)
        return rows

    def test_the_forest_never_trains_on_the_month_it_forecasts(self):
        class LatestDaySeen:
            """Forecasts the latest day it was trained on, so each forecast
            reveals what the model was allowed to see."""
            def fit(self, X, y):
                self.latest = X[:, 0].max()
                return self

            def predict(self, X):
                return [self.latest] * len(X)

        rows = self.rows(date(2026, 1, 1), date(2026, 3, 31), actual=lambda d, h: 0.5,
                         curve=lambda d, h: 0.5, features=lambda d, h: [float(d.toordinal())])
        forecasts = forest.walk_forward(rows, date(2026, 2, 1), date(2026, 3, 31),
                                        model_factory=LatestDaySeen)

        self.assertEqual({f.day.month for f in forecasts}, {2, 3})
        for f in forecasts:
            self.assertLess(date.fromordinal(int(f.forest)), f.day.replace(day=1))

    def test_days_the_dashboard_would_not_forecast_are_not_scored(self):
        class Constant:
            def fit(self, X, y):
                return self

            def predict(self, X):
                return [0.5] * len(X)

        rows = self.rows(date(2026, 1, 1), date(2026, 3, 31), actual=lambda d, h: 0.5,
                         curve=lambda d, h: 0.5, features=lambda d, h: [float(h)],
                         weeks=lambda d: 2 if d.month == 3 else 8)
        forecasts = forest.walk_forward(rows, date(2026, 2, 1), date(2026, 3, 31), model_factory=Constant)

        self.assertEqual({f.day.month for f in forecasts}, {2},
                         "below three instances the dashboard shows no curve, so there is none to beat")

    def test_it_can_learn_what_a_median_of_past_weeks_cannot(self):
        """The first three weeks of a term run 30% hotter. The curve cannot see
        where in the term a day falls; the forest is told."""
        from sklearn.ensemble import RandomForestRegressor

        def base(hour):
            return 0.2 + 0.6 * (8 <= hour <= 20)

        def since_term(day):
            return (day - (date(2026, 1, 13) if day >= date(2026, 1, 13) else date(2025, 8, 20))).days

        rows = self.rows(date(2025, 8, 20), date(2026, 2, 28),
                         actual=lambda d, h: base(h) * (1.3 if since_term(d) < 21 else 1.0),
                         curve=lambda d, h: base(h),
                         features=lambda d, h: [float(h), float(since_term(d))])
        forecasts = forest.walk_forward(
            rows, date(2026, 1, 1), date(2026, 2, 28),
            model_factory=lambda: RandomForestRegressor(n_estimators=25, min_samples_leaf=5, random_state=0))
        [overall] = [c for c in forest.compare(forest.per_day(forecasts)) if c.kind == "all"]

        self.assertLess(overall.forest_error, overall.curve_error / 2)


if __name__ == "__main__":
    unittest.main()
