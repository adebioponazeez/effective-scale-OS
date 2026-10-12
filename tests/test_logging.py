"""Structured logging contract: severity is real, payload survives, secrets are redacted.

Call sites mark severity with a flag (`logger.log(event, warn=True, error=str(exc))`).
That flag must become the record's `level`, not a stray payload field — otherwise
every warning and error in the system is stored as `info` and a `level="warn"`
logger silently drops the incident trail.
"""
from __future__ import annotations

import io
import json
import unittest

from effective_scale.ports.logger import JsonLogger, MemLogger, redact, split_level


class SplitLevelTest(unittest.TestCase):
    def test_boolean_flag_becomes_the_level_and_is_consumed(self):
        level, fields = split_level("info", {"warn": True, "workflow": "w1"})
        self.assertEqual(level, "warn")
        self.assertEqual(fields, {"workflow": "w1"})

    def test_explicit_level_wins_but_the_flag_is_still_consumed(self):
        level, fields = split_level("error", {"warn": True, "workflow": "w1"})
        self.assertEqual(level, "error")
        self.assertEqual(fields, {"workflow": "w1"})

    def test_non_boolean_error_stays_a_payload_field(self):
        level, fields = split_level("info", {"error": "boom", "warn": True})
        self.assertEqual(level, "warn")
        self.assertEqual(fields, {"error": "boom"}, "an error string is payload, not severity")

    def test_unknown_fields_are_untouched(self):
        level, fields = split_level("info", {"crash": True})
        self.assertEqual(level, "info")
        self.assertEqual(fields, {"crash": True})


class MemLoggerTest(unittest.TestCase):
    def test_records_carry_real_levels(self):
        log = MemLogger()
        log.log("leader.demoted", warn=True, holder="a", reason="renew_failed")
        log.log("workflow.deadletter", warn=True, workflow="w1", error="permanent failure")
        log.log("leader.acquired", info=True, holder="a")
        self.assertEqual([r["level"] for r in log.records], ["warn", "warn", "info"])
        self.assertEqual(log.records[1]["error"], "permanent failure")
        self.assertNotIn("warn", log.records[0], "the level flag must not leak into the record")


class JsonLoggerTest(unittest.TestCase):
    def _logger(self, level: str = "info", redact_secrets: bool = True):
        stream = io.StringIO()
        return stream, JsonLogger(stream=stream, level=level, redact_secrets=redact_secrets)

    def _records(self, stream: io.StringIO) -> list[dict]:
        return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]

    def test_level_filter_keeps_warnings_and_errors(self):
        """A warn floor must drop info chatter — and keep the records that matter."""
        stream, log = self._logger(level="warn")
        log.log("routine.tick", info=True)
        log.log("leader.demoted", warn=True, holder="a")
        log.log("write.failed", error=True, error_code=500)
        records = self._records(stream)
        self.assertEqual([r["event"] for r in records], ["leader.demoted", "write.failed"])
        self.assertEqual([r["level"] for r in records], ["warn", "error"])

    def test_error_floor_is_not_empty(self):
        stream, log = self._logger(level="error")
        log.log("workflow.finalize_publish_failed", error=True, workflow="w1")
        records = self._records(stream)
        self.assertEqual(len(records), 1, "errors must survive an error-level floor")
        self.assertEqual(records[0]["level"], "error")

    def test_record_shape_and_secret_redaction(self):
        stream, log = self._logger()
        log.log("token.minted", level="info", holder="a", token="super-secret",
                nested={"api_key": "k", "keep": 1})
        record = self._records(stream)[0]
        self.assertEqual(set(record) >= {"ts", "level", "event"}, True)
        self.assertEqual(record["level"], "info")
        self.assertEqual(record["token"], "[REDACTED]")
        self.assertEqual(record["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(record["nested"]["keep"], 1)

    def test_redaction_helper_handles_depth_and_lists(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}
        self.assertEqual(redact(deep)["a"]["b"]["c"]["d"]["e"]["f"]["g"], "<truncated>")
        self.assertEqual(redact({"list": [{"password": "p"}, "plain"]}),
                         {"list": [{"password": "[REDACTED]"}, "plain"]})


if __name__ == "__main__":
    unittest.main()
