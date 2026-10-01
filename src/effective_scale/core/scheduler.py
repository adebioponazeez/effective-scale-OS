"""Scheduler: a pure function (snapshot, now) -> Plan.

Placement constraint order (hard -> soft):
  1. node state == active (cordoned/draining never receive new leases)
  2. tags: workload.node_tags must be a subset of node.tags
  3. pin: node id must match exactly
  4. anti-affinity/spread: avoid a second replica on the same node when alternative exists
  5. capacity: cpu + memory must fit
  6. policy order (each policy is real; none falls back to another):
       bin_pack    - most free resources first (minimise fragmentation)
       round_robin - fewest active leases first (even spread)
       fifo        - earliest-created eligible node first (queue-like fill, no reshuffle)
       priority    - affinity to nodes already running this workload, then least loaded
                     (keeps a high-priority service together instead of churning nodes)

Scale-down prefers leases on draining nodes first, then oldest first — deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.models import Lease, Node, SchedulingPolicy, Workload
from ..domain.states import LeaseState, NodeState, WorkloadStatus
from ..ports.store import Snapshot

DEFAULT_LEASE_TTL = 300.0


@dataclass(frozen=True)
class Grant:
    workload_id: str
    node_id: str
    ttl: float = DEFAULT_LEASE_TTL


@dataclass(frozen=True)
class Release:
    lease_id: str


@dataclass(frozen=True)
class Plan:
    grants: list[Grant] = field(default_factory=list)
    releases: list[Release] = field(default_factory=list)


def _node_load(snap: Snapshot, now: float) -> dict[str, tuple[int, int, int]]:
    """node_id -> (active leases, cpu used, mem used)."""
    out: dict[str, list] = {nid: [0, 0, 0] for nid in snap.nodes}
    for lease in snap.leases.values():
        if lease.state != LeaseState.ACTIVE:
            continue
        if lease.expires_at <= now:
            continue
        wl = snap.workloads.get(lease.workload_id)
        if wl is None or wl.status != WorkloadStatus.ACTIVE:
            continue
        entry = out.setdefault(lease.node_id, [0, 0, 0])
        entry[0] += 1
        entry[1] += wl.cpu
        entry[2] += wl.memory
    return out


def _eligible_nodes(snap: Snapshot, wl: Workload, load: dict[str, list]) -> list[Node]:
    nodes: list[Node] = []
    for node in snap.nodes.values():
        if node.state != NodeState.ACTIVE:
            continue
        if wl.pin and wl.pin != node.id:
            continue
        if not set(wl.node_tags).issubset(set(node.tags)):
            continue
        # load[node.id] = (active leases, cpu used, mem used) — compare like for like,
        # otherwise placement silently overcommits (see K23 / tests/test_scheduler.py)
        _, cpu_used, mem_used = load[node.id]
        if node.capacity_cpu - cpu_used < wl.cpu or node.capacity_mem - mem_used < wl.memory:
            continue
        if wl.spread and load[node.id][0] > 0:
            continue  # anti-affinity: another node should host the next replica
        nodes.append(node)
    return nodes


def _pick(snap: Snapshot, wl: Workload, load: dict[str, list], policy: SchedulingPolicy) -> Node | None:
    nodes = _eligible_nodes(snap, wl, load)
    if not nodes:
        return None

    def key(node: Node):
        l = load[node.id]
        return (l[0], l[1], node.id)

    if policy == SchedulingPolicy.BIN_PACK:
        # prefer emptiest (most free) node; pack tightly by resource
        nodes.sort(key=lambda n: (-(n.capacity_cpu - load[n.id][1]), n.id))
    elif policy == SchedulingPolicy.ROUND_ROBIN:
        nodes.sort(key=lambda n: (load[n.id][0], n.id))
    elif policy == SchedulingPolicy.FIFO:
        # queue-like fill: earliest-created eligible node first, until it is full
        nodes.sort(key=lambda n: (n.created_at, n.id))
    elif policy == SchedulingPolicy.PRIORITY:
        # affinity: stay on nodes already hosting this workload, then least loaded
        holders = {
            lease.node_id for lease in snap.leases.values()
            if lease.workload_id == wl.id and lease.state == LeaseState.ACTIVE
        }
        nodes.sort(key=lambda n: (n.id not in holders, load[n.id][0], load[n.id][1], n.id))
    else:  # unknown policy (older records): deterministic least-loaded first-fit
        nodes.sort(key=lambda n: (load[n.id][1], n.id))
    return nodes[0]


class Scheduler:
    """compute() is pure and deterministic; apply() is owned by the kernel writer."""

    def __init__(self, lease_ttl: float = DEFAULT_LEASE_TTL):
        self.lease_ttl = lease_ttl

    def compute(self, snap: Snapshot, now: float) -> Plan:
        load = _node_load(snap, now)
        grants: list[Grant] = []
        releases: list[Release] = []

        workloads = [
            w for w in snap.workloads.values()
            if w.status == WorkloadStatus.ACTIVE
        ]
        workloads.sort(key=lambda w: (-w.priority, w.created_at, w.id))

        active_by_wl: dict[str, list[Lease]] = {}
        for lease in snap.leases.values():
            if lease.state == LeaseState.ACTIVE and lease.expires_at > now:
                active_by_wl.setdefault(lease.workload_id, []).append(lease)

        for wl in workloads:
            current = len(active_by_wl.get(wl.id, []))
            desired = wl.desired_replicas

            if desired > current:
                # re-evaluate load fresh as we place grants
                for _ in range(desired - current):
                    node = _pick(snap, wl, load, wl.policy)
                    if node is None:
                        break  # capacity exhausted — backlog remains observable via desired>actual
                    grants.append(Grant(workload_id=wl.id, node_id=node.id, ttl=self.lease_ttl))
                    load.setdefault(node.id, [0, 0, 0])
                    load[node.id][0] += 1
                    load[node.id][1] += wl.cpu
                    load[node.id][2] += wl.memory

            if desired < current:
                leases = sorted(
                    active_by_wl[wl.id],
                    key=lambda l: (
                        snap.nodes.get(l.node_id).state != NodeState.DRAINING,
                        l.created_at,
                        l.id,
                    ),
                )
                for lease in leases[: current - desired]:
                    releases.append(Release(lease_id=lease.id))

        return Plan(grants=grants, releases=releases)
