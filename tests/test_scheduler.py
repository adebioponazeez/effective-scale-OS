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

    # --- capacity accounting: CPU and memory are separate budgets (K23) ---

    def test_memory_budget_is_enforced(self):
        """Plenty of CPU but no memory room: the node must not be used."""
        _node(self.snap, "fat-cpu", cpu=100_000, mem=64)   # 64 < the 128 the pod needs
        _wl(self.snap, "w1", 1, cpu=100, mem=128)
        plan = self.sched.compute(self.snap, now=2000.0)
        self.assertEqual(plan.grants, [], "memory overcommit was accepted")

    def test_cpu_budget_is_enforced(self):
        """Plenty of memory but no CPU room: the node must not be used."""
        _node(self.snap, "fat-mem", cpu=150, mem=1_000_000)
        _wl(self.snap, "w1", 1, cpu=100, mem=128)
        plan = self.sched.compute(self.snap, now=2000.0)
        self.assertEqual(len(plan.grants), 1)
        # the second replica cannot fit: only 50 cpu remain
        _wl(self.snap, "w2", 1, cpu=100, mem=128)
        plan2 = self.sched.compute(self.snap, now=2000.0)
        grants = [g for g in plan2.grants if g.workload_id == "w2"]
        self.assertEqual(grants, [], "cpu overcommit was accepted")

    def test_a_lease_count_is_not_a_cpu_budget(self):
        """Regression: a node with many tiny leases must still be judged on real usage."""
        from effective_scale.domain.models import Lease
        from effective_scale.domain.states import LeaseState
        _node(self.snap, "busy", cpu=10_000, mem=10_000)
        filler = _wl(self.snap, "filler", 6, cpu=10, mem=10)  # 6 tiny leases on one node
        self.snap.workloads["filler"] = filler
        for i in range(6):
            self.snap.leases[f"f{i}"] = Lease(id=f"f{i}", workload_id="filler", namespace="ns",
                                              node_id="busy", holder="h", nonce="n",
                                              expires_at=9999, state=LeaseState.ACTIVE,
                                              created_at=10.0 + i)
        _wl(self.snap, "big", 1, cpu=9000, mem=9000)
        plan = self.sched.compute(self.snap, now=2000.0)
        grant = next(g for g in plan.grants if g.workload_id == "big")
        self.assertEqual(grant.node_id, "busy", "6 leases x 10 units must leave room for 9000")

    # --- each policy is real: none may fall back to another (GAP-AUDIT B-4) ---

    def test_fifo_fills_earliest_created_node_first(self):
        """FIFO is queue-like: fill the earliest node until it is full, then move on."""
        _node(self.snap, "cli7", cpu=150, mem=1024)   # room for exactly one replica
        _node(self.snap, "api2", cpu=1000, mem=1024)  # room for several
        self.snap.nodes["api2"].created_at = 2000.0   # api2 joined later
        _wl(self.snap, "w1", 2, cpu=100, mem=128, policy="fifo")
        plan = self.sched.compute(self.snap, now=3000.0)
        self.assertEqual([g.node_id for g in plan.grants], ["cli7", "api2"])

    def test_fifo_packs_the_earliest_node_by_default(self):
        """Queue semantics: two replicas that fit on the oldest node both go there."""
        _node(self.snap, "cli7", cpu=1000, mem=1024)
        _node(self.snap, "api2", cpu=1000, mem=1024)
        self.snap.nodes["api2"].created_at = 2000.0
        _wl(self.snap, "w1", 2, cpu=100, mem=128, policy="fifo")
        plan = self.sched.compute(self.snap, now=3000.0)
        self.assertEqual([g.node_id for g in plan.grants], ["cli7", "cli7"])

    def test_fifo_is_not_bin_pack(self):
        """Pin the difference: bin-pack orders by free capacity, FIFO by node age."""
        _node(self.snap, "aaa", cpu=1000, mem=1024)   # alphabetically first
        _node(self.snap, "zzz", cpu=1000, mem=1024)
        self.snap.nodes["aaa"].created_at = 2000.0
        self.snap.nodes["zzz"].created_at = 1000.0    # older
        _wl(self.snap, "fifo-wl", 1, policy="fifo")
        _wl(self.snap, "pack-wl", 1, policy="bin_pack")
        plan = self.sched.compute(self.snap, now=3000.0)
        picked = {g.workload_id: g.node_id for g in plan.grants}
        self.assertEqual(picked["fifo-wl"], "zzz", "fifo must follow node age")
        self.assertNotEqual(picked["pack-wl"], "zzz",
                            "bin_pack must not silently behave like fifo")

    def test_round_robin_spreads_by_lease_count(self):
        """A node already holding a lease is skipped in favour of an idle one."""
        from effective_scale.domain.models import Lease
        from effective_scale.domain.states import LeaseState
        _node(self.snap, "a")
        _node(self.snap, "b")
        other = _wl(self.snap, "other", 1)
        self.snap.leases["l1"] = Lease(id="l1", workload_id="other", namespace="ns", node_id="a",
                                       holder="h", nonce="n", expires_at=9999,
                                       state=LeaseState.ACTIVE, created_at=10.0)
        self.assertEqual(other.id, "other")
        _wl(self.snap, "w1", 1, policy="round_robin")
        plan = self.sched.compute(self.snap, now=3000.0)
        grant = next(g for g in plan.grants if g.workload_id == "w1")
        self.assertEqual(grant.node_id, "b")

    def test_priority_prefers_the_node_already_running_this_workload(self):
        """Affinity keeps a high-priority service together instead of churning nodes."""
        from effective_scale.domain.models import Lease
        from effective_scale.domain.states import LeaseState
        _node(self.snap, "a")
        _node(self.snap, "b")
        _node(self.snap, "c")
        wl = _wl(self.snap, "w1", 2, policy="priority", priority=10)
        self.snap.leases["l1"] = Lease(id="l1", workload_id=wl.id, namespace="ns", node_id="c",
                                       holder="h", nonce="n", expires_at=9999,
                                       state=LeaseState.ACTIVE, created_at=10.0)
        plan = self.sched.compute(self.snap, now=3000.0)
        grant = next(g for g in plan.grants if g.workload_id == wl.id)
        self.assertEqual(grant.node_id, "c", "priority must reuse a host already running the service")

    def test_unknown_legacy_policy_is_deterministic(self):
        """Older records may carry a policy this build no longer knows: never crash, never random."""
        _node(self.snap, "a")
        _node(self.snap, "b")
        wl = _wl(self.snap, "w1", 1)
        wl.policy = "legacy_policy_from_a_future_build"  # type: ignore[assignment]
        first = self.sched.compute(self.snap, now=3000.0)
        second = self.sched.compute(self.snap, now=3000.0)
        self.assertEqual(first, second)
        self.assertEqual(len(first.grants), 1)

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
