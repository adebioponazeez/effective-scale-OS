import unittest

from effective_scale.core.scaler import TargetScaler
from effective_scale.domain.models import Workload
from tests.helpers import FakeClock


def _wl(desired: int, *, min_r=1, max_r=10, cpu_target=0.65, cooldown=60.0):
    return Workload.create("ns", {
        "name": "web", "image": "img", "replicas": desired, "min_replicas": min_r,
        "max_replicas": max_r, "cpu": 100, "memory": 128,
        "scaler": {"cooldown_seconds": cooldown, "metrics": {"cpu": {"target": cpu_target}}},
    }, now=0.0)


class ScalerTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.scaler = TargetScaler(cooldown=60.0, protect_after=180.0, step=4)

    def test_scales_up_when_above_target(self):
        wl = _wl(2)
        new = self.scaler.decide(wl, {"cpu": 0.9}, now=1000.0)
        self.assertIsNotNone(new)
        self.assertEqual(new, 3)  # ceil(2 * 0.9/0.65) = 3

    def test_deadband_suppresses_noise(self):
        wl = _wl(10)
        new = self.scaler.decide(wl, {"cpu": 0.67}, now=1000.0)
        self.assertIsNone(new)  # 1.03 => within 10% deadband

    def test_cooldown_blocks_rapid_decisions(self):
        wl = _wl(2)
        self.scaler.decide(wl, {"cpu": 0.9}, now=1000.0)
        self.scaler.record(wl.id, 1000.0, 3, 2)
        self.assertIsNone(self.scaler.decide(wl, {"cpu": 1.5}, now=1030.0))

    def test_scale_down_protection(self):
        wl = _wl(5)
        self.scaler.record(wl.id, 900.0, 5, 2)  # up-scaled recently
        self.assertIsNone(self.scaler.decide(wl, {"cpu": 0.3}, now=1000.0))
        self.assertIsNotNone(self.scaler.decide(wl, {"cpu": 0.3}, now=1100.0))

    def test_clamped_to_bounds(self):
        wl = _wl(2, min_r=1, max_r=3)
        new = self.scaler.decide(wl, {"cpu": 5.0}, now=1000.0)
        self.assertEqual(new, 3)

    def test_step_limit(self):
        wl = _wl(1)
        new = self.scaler.decide(wl, {"cpu": 10.0}, now=1000.0)
        self.assertEqual(new, 5)  # step=4 from 1

    def test_multiple_metrics_take_max_demand(self):
        from effective_scale.core.scaler import TargetScaler as TS

        scaler = TS(cooldown=60.0, protect_after=180.0)
        wl = Workload.create("ns", {
            "name": "w", "image": "img", "replicas": 2, "max_replicas": 200,
            "scaler": {"metrics": {"cpu": {"target": 0.65}, "queue": {"target": 10}}},
        }, now=0.0)
        new = scaler.decide(wl, {"cpu": 0.4, "queue": 600}, now=1000.0)
        self.assertEqual(new, 120)  # dominant metric is queue demand

    def test_no_config_no_decision(self):
        wl = Workload.create("ns", {"name": "w", "image": "img", "replicas": 2}, now=0.0)
        self.assertIsNone(self.scaler.decide(wl, {"cpu": 5.0}, now=1000.0))


if __name__ == "__main__":
    unittest.main()
