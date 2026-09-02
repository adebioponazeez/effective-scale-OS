import unittest

from effective_scale.core.scheduler import Scheduler
from effective_scale.domain.models import Node, Workload
from effective_scale.ports.store import Snapshot


def _wl(snap: Snapshot, wid: str, desired: int, *, cpu: int = 100, mem: int = 128,
        priority: int = 0, policy: str = "bin_pack", tags=(), spread=False,
        pin=None) -> Workload:
    wl = Workload.create("ns", {
        "name": wid, "image": "img", "replicas": desired, "cpu": cpu, "memory": mem,
        "priority": priority, "policy": policy, "node_tags": list(tags),
        "spread": spread, "pin": pin,
    }, now=1000.0)
    wl.id = wid
    snap.workloads[wid] = wl
    return wl


def _node(snap: Snapshot, nid: str, *, cpu: int = 1000, mem: int = 1024,
          tags=(), state=None) -> Node:
    node = Node.create({"name": nid, "cpu": cpu, "memory": mem, "tags": list(tags)}, now=1000.0)
    node.id = nid
    snap.nodes[nid] = node
    if state is not None:
        node.state = state
    return node


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.snap = Snapshot()
        self.sched = Scheduler(lease_ttl=300)

    def test_bin_pack_places_on_least_loaded(self):
        _node(self.snap, "a", cpu=1000, mem=1024)
        _node(self.snap, "b", cpu=1000, mem=1024)
        wl = _wl(self.snap, "w1", 2)
        plan = self.sched.compute(self.snap, now=2000.0)
        self.assertEqual(len(plan.grants), 2)
        # bin-pack: second grant goes to the least-loaded node
        self.assertEqual(len({g.node_id for g in plan.grants}), 2)

    def test_capacity_respected(self):
        _node(self.snap, "small", cpu=150, mem=128)
        _wl(self.snap, "w1", 2, cpu=100, mem=128)
        plan = self.sched.compute(self.snap, now=2000.0)
        self.assertEqual(len(plan.grants), 1)  # only one fits

    def test_cordoned_and_draining_nodes_never_receive(self):
        _node(self.snap, "c", state="cordoned")
        _node(self.snap, "d", state="draining")
        _wl(self.snap, "w1", 1)
        plan = self.sched.compute(self.snap, now=2000.0)
        self.assertEqual(plan.grants, [])

    def test_tag_and_pin_constraints(self):
        _node(self.snap, "prod-a", tags=("prod",))
        _node(self.snap, "dev-a", tags=("dev",))
        _wl(self.snap, "w1", 1, tags=("prod",))
        plan = self.sched.compute(self.snap, now=2000.0)
        self.assertEqual(plan.grants[0].node_id, "prod-a")
        del self.snap.workloads["w1"]
        wl2 = _wl(self.snap, "w2", 1, pin="dev-a")
        plan2 = self.sched.compute(self.snap, now=2000.0)
        self.assertEqual([g.node_id for g in plan2.grants], ["dev-a"])

    def test_spread_anti_affinity(self):
        _node(self.snap, "a", cpu=10_000, mem=10_000)
        _node(self.snap, "b", cpu=10_000, mem=10_000)
        wl = _wl(self.snap, "w1", 2, spread=True)
        plan = self.sched.compute(self.snap, now=2000.0)
        nodes = [g.node_id for g in plan.grants]
        self.assertEqual(len(set(nodes)), 2, "spread must place replicas on distinct nodes")

    def test_scale_down_leases_sorted_oldest_first(self):
        _node(self.snap, "a", cpu=10_000, mem=10_000)
        wl = _wl(self.snap, "w1", 3)
        plan = self.sched.compute(self.snap, now=2000.0)
        self.assertEqual(len(plan.grants), 3)
        # simulate: three leases created sequentially
        from effective_scale.domain.models import Lease
        from effective_scale.domain.states import LeaseState
        for i in range(3):
            lease = Lease(id=f"l{i}", workload_id="w1", namespace="ns", node_id="a",
                          holder="h", nonce="n", expires_at=9999,
                          state=LeaseState.ACTIVE, created_at=1000 + i)
            self.snap.leases[lease.id] = lease
        wl.desired_replicas = 1
        plan2 = self.sched.compute(self.snap, now=2000.0)
        self.assertEqual(len(plan2.releases), 2)
        self.assertEqual([r.lease_id for r in plan2.releases], ["l0", "l1"])

    def test_deterministic_tie_break(self):
        _node(self.snap, "a", cpu=1000, mem=1024)
        _node(self.snap, "b", cpu=1000, mem=1024)
        _wl(self.snap, "w1", 1)
        p1 = self.sched.compute(self.snap, now=2000.0)
        p2 = self.sched.compute(self.snap, now=2000.0)
        self.assertEqual(p1, p2)

    def test_priority_order(self):
        _node(self.snap, "a", cpu=1000, mem=1024)
        _node(self.snap, "b", cpu=1000, mem=1024)
        low = _wl(self.snap, "low", 1, priority=0)
        high = _wl(self.snap, "high", 1, priority=10)
        plan = self.sched.compute(self.snap, now=2000.0)
        # high priority gets the first grant; both fit so both granted
        self.assertEqual(len(plan.grants), 2)
        first = plan.grants[0]
        self.assertEqual(first.workload_id, "high")

    def test_no_grants_when_desired_met(self):
        _node(self.snap, "a", cpu=1000, mem=1024)
        wl = _wl(self.snap, "w1", 1)
        from effective_scale.domain.models import Lease
        from effective_scale.domain.states import LeaseState
        self.snap.leases["l1"] = Lease(id="l1", workload_id="w1", namespace="ns", node_id="a",
                                       holder="h", nonce="n", expires_at=9999,
                                       state=LeaseState.ACTIVE)
        plan = self.sched.compute(self.snap, now=2000.0)
        self.assertEqual(plan.grants, [])


if __name__ == "__main__":
    unittest.main()
