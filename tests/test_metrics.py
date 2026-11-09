import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strata_monitor.metrics import (activity_label, adaptive_metrics, model_name,
                                    responsive_scale, tokens_per_second)


def tasks(count, start, *, finish="stop", duration=4):
    return [{"time": start + i, "duration_s": duration, "finish": finish} for i in range(count)]


class AdaptiveMetricsTests(unittest.TestCase):
    NOW = 10_000

    def test_selects_ten_minutes_at_ten_verified_tasks(self):
        result = adaptive_metrics(tasks(10, self.NOW - 500) + tasks(4, self.NOW - 1000, finish="error"), self.NOW)
        self.assertEqual(result["window_minutes"], 10)
        self.assertFalse(result["insufficient_data"])
        self.assertEqual((result["tts_seconds"], result["tts_samples"], result["tsr_percent"], result["tsr_samples"]),
                         (4, 10, 100.0, 10))

    def test_expands_to_twenty_then_thirty_minutes(self):
        ten = tasks(9, self.NOW - 400) + tasks(2, self.NOW - 700)
        self.assertEqual(adaptive_metrics(ten, self.NOW)["window_minutes"], 20)
        twenty = tasks(9, self.NOW - 700)
        twenty += tasks(1, self.NOW - 1300)
        self.assertEqual(adaptive_metrics(twenty, self.NOW)["window_minutes"], 30)

    def test_thirty_minute_window_marks_insufficient_samples(self):
        result = adaptive_metrics(tasks(2, self.NOW - 1700), self.NOW)
        self.assertEqual(result["window_minutes"], 30)
        self.assertTrue(result["insufficient_data"])
        self.assertEqual(result["tsr_samples"], 2)

    def test_window_recalculates_as_recent_tasks_expire(self):
        records = tasks(10, self.NOW - 500)
        self.assertEqual(adaptive_metrics(records, self.NOW)["window_minutes"], 10)
        result = adaptive_metrics(records, self.NOW + 200)
        self.assertEqual(result["window_minutes"], 20)
        self.assertFalse(result["insufficient_data"])
        result = adaptive_metrics(records, self.NOW + 2000)
        self.assertEqual(result["window_minutes"], 30)
        self.assertTrue(result["insufficient_data"])

    def test_expired_incomplete_unknown_and_future_rows_do_not_count(self):
        records = tasks(9, self.NOW - 700) + tasks(1, self.NOW - 1000)
        records += tasks(3, self.NOW - 1900)
        records += [
            {"time": self.NOW - 1, "duration_s": 0.5, "finish": "in_progress"},
            {"time": self.NOW + 20, "duration_s": 0.5, "finish": "stop"},
            {"at": "invalid", "durationMs": 10, "state": "done"},
        ]
        result = adaptive_metrics(records, self.NOW)
        self.assertEqual(result["window_minutes"], 20)
        self.assertEqual(result["tsr_samples"], 10)

    def test_empty_window_uses_na_values(self):
        result = adaptive_metrics([], self.NOW)
        self.assertEqual(result["window_minutes"], 30)
        self.assertTrue(result["insufficient_data"])
        self.assertIsNone(result["tts_seconds"])
        self.assertIsNone(result["tsr_percent"])
        self.assertEqual(result["tts_samples"], 0)
        self.assertEqual(result["tsr_samples"], 0)
        self.assertIsNone(result["ttft_ms"])
        self.assertEqual(result["ttft_samples"], 0)

    def test_ttft_averages_only_reported_samples_in_the_selected_window(self):
        outcome_rows = tasks(10, self.NOW - 20)
        ttft_rows = [
            {"at": "1970-01-01T02:46:20+00:00", "durationMs": 5000, "state": "done", "ttftMs": 1000},
            {"at": "1970-01-01T02:46:21+00:00", "durationMs": 5000, "state": "done", "ttft_ms": 3000},
            {"at": "1970-01-01T02:20:00+00:00", "durationMs": 5000, "state": "done", "ttftMs": 9000},
            {"at": "1970-01-01T02:46:22+00:00", "durationMs": 5000, "state": "done"},
        ]
        result = adaptive_metrics(outcome_rows, self.NOW, ttft_requests=ttft_rows)
        self.assertEqual(result["window_minutes"], 10)
        self.assertEqual(result["ttft_ms"], 2000)
        self.assertEqual(result["ttft_samples"], 2)

    def test_success_rate_uses_only_verified_outcomes(self):
        records = tasks(9, self.NOW - 20)
        records += tasks(1, self.NOW - 30, finish="error")
        records += [{"time": self.NOW - 5, "duration_s": 2, "finish": "mystery"}]
        result = adaptive_metrics(records, self.NOW)
        self.assertEqual(result["tsr_samples"], 10)
        self.assertEqual(result["tsr_percent"], 90)
        self.assertEqual(result["tts_samples"], 9)

    def test_host_monitor_rows_support_iso_dates_and_explicit_states(self):
        records = [
            {"at": "1970-01-01T02:40:00+00:00", "durationMs": 2000, "state": "done"},
            {"at": "1970-01-01T02:40:03+00:00", "durationMs": 4000, "state": "error"},
        ]
        result = adaptive_metrics(records, 10_000)
        self.assertEqual(result["tsr_samples"], 2)
        self.assertEqual(result["tsr_percent"], 50)
        self.assertEqual(result["tts_seconds"], 2)


class ActivityTests(unittest.TestCase):
    def test_widget_scaling_clamps_from_compact_to_large_dimensions(self):
        self.assertAlmostEqual(responsive_scale(190, 175), 175 / 190)
        self.assertEqual(responsive_scale(200, 190), 1.0)
        self.assertEqual(responsive_scale(1920, 1080), 1.12)

    def test_only_reports_observed_activity(self):
        self.assertEqual(activity_label({"live": {"state": "generating"}}), "Generating")
        self.assertEqual(activity_label({"phase": "reading"}), "Reading")
        self.assertEqual(activity_label({"phase": "idle"}, {"activeTurns": 1}), "Active")
        self.assertEqual(activity_label({"phase": "idle"}), "Idle")
        self.assertEqual(activity_label({"phase": "idle"}, {"activeTurns": "unknown"}), "Idle")
        self.assertEqual(activity_label({}), "Unknown")

    def test_throughput_reports_generation_or_prompt_reading_rates_only(self):
        self.assertEqual(tokens_per_second({"live": {"state": "generating", "tok_s": 50,
                                                       "tok_s_mean": 37.5}}), 37.5)
        self.assertIsNone(tokens_per_second({"live": {"state": "generating", "tok_s": 50}}))
        self.assertEqual(tokens_per_second({"live": {"state": "reading", "prefill_tok_s_mean": 2400}}), 2400)
        self.assertEqual(tokens_per_second({"phase": "reading", "promptTps": 1800}), 1800)
        self.assertIsNone(tokens_per_second({"live": {"state": "reading"}, "outputTps": 50}))
        self.assertEqual(tokens_per_second({"live": {"state": "idle", "tok_s": 50}}), 0)
        self.assertIsNone(tokens_per_second({"live": {"state": "generating"}}))
        self.assertIsNone(tokens_per_second({"phase": "generating", "outputTps": 50,
                                             "telemetry": {"throughput": "demo"}}))
        self.assertIsNone(tokens_per_second({}))

    def test_model_name_uses_reported_model_identity(self):
        self.assertEqual(model_name({"engine": {"model": "Qwen Coder IQ1_M"}}), "Qwen Coder IQ1_M")
        self.assertEqual(model_name({}), "N/A")
        self.assertEqual(model_name({"variantId": "cached", "engine": {"running": False}}), "N/A")


if __name__ == "__main__":
    unittest.main()
