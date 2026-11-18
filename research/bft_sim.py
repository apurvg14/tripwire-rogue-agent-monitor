"""Ledger keepers (replicas) agreeing on each entry, with some of them Byzantine.

Each step, two transactions arrive at once: an `add` and a currency `convert`.
Either order is legitimate, but the order changes the result, so a crooked leader
can tell half the keepers "add first" and the other half "convert first". Both
proposals pass validation, so validation alone can't stop this. Only voting can.
"""
import random
from collections import Counter, defaultdict
from decimal import Decimal

CENT = Decimal("0.01")
ORDERS = [("add", "convert"), ("convert", "add")]


def apply(value, order, txs):
    for name in order:
        t = txs[name]
        value = value + t["amount"] if t["op"] == "add" else (value * t["rate"]).quantize(CENT)
    return value


class Replica:
    def __init__(self, rid, byzantine=False):
        self.id, self.byzantine = rid, byzantine
        self.ledger = [Decimal("10000.00")]

    def options(self, txs):
        return [(order, apply(self.ledger[-1], order, txs)) for order in ORDERS]

    def valid(self, proposal, txs):
        return proposal in self.options(txs)


def pre_prepare(leader, honest, txs, rng):
    """What the leader sends each honest replica. A Byzantine leader picks a dirty trick."""
    if not leader.byzantine:
        p = leader.options(txs)[0]
        return {r.id: p for r in honest}
    move = rng.choices(["equivocate", "tamper", "silent"], [0.6, 0.2, 0.2])[0]
    if move == "equivocate":
        shuffled = rng.sample(honest, len(honest))
        cut = rng.randint(1, len(honest) - 1)
        return {r.id: r.options(txs)[0 if i < cut else 1] for i, r in enumerate(shuffled)}
    if move == "tamper":
        return {r.id: (r.options(txs)[0][0], r.options(txs)[0][1] + 100) for r in honest}
    return {}


def naive_step(replicas, leader, txs, f, rng):
    honest = [r for r in replicas if not r.byzantine]
    received = pre_prepare(leader, honest, txs, rng)
    accepted = False
    for r in honest:
        p = received.get(r.id)
        if p is not None and r.valid(p, txs):
            r.ledger.append(p[1])
            accepted = True
    return accepted


def pbft_step(replicas, leader, txs, f, rng):
    quorum = 2 * f + 1
    honest = [r for r in replicas if not r.byzantine]
    byz = [r for r in replicas if r.byzantine]
    received = pre_prepare(leader, honest, txs, rng)

    def broadcast(votes_from_honest):
        """Honest replicas send their vote to everyone. Byzantine replicas tell each
        honest replica whatever that replica already believes, to push every group
        toward its own (conflicting) quorum."""
        inbox = defaultdict(Counter)
        for p in votes_from_honest.values():
            for t in honest:
                inbox[t.id][p] += 1
        for _ in byz:
            for t in honest:
                if t.id in received:
                    inbox[t.id][received[t.id]] += 1
        return inbox

    accepted = {r.id: p for r in honest
                if (p := received.get(r.id)) is not None and r.valid(p, txs)}
    prepares = broadcast(accepted)
    prepared = {rid: p for rid, p in accepted.items() if prepares[rid][p] >= quorum}
    commits = broadcast(prepared)
    decided = {rid: p for rid, p in prepared.items() if commits[rid][p] >= quorum}
    if not decided:
        return False

    announcements = broadcast(decided)
    for r in honest:
        if r.id in decided:
            r.ledger.append(decided[r.id][1])
            continue
        for p, count in announcements[r.id].items():
            if count >= f + 1 and r.valid(p, txs):
                r.ledger.append(p[1])
                break
    return True


def run(seed, n, n_byz, protocol, steps=50):
    rng = random.Random(seed)
    byz_ids = set(rng.sample(range(n), n_byz))
    replicas = [Replica(i, i in byz_ids) for i in range(n)]
    f = (n - 1) // 3
    step_fn = pbft_step if protocol == "pbft" else naive_step

    view, view_changes, stalled = 0, 0, 0
    for step in range(steps):
        txs = {"add": {"op": "add", "amount": Decimal(rng.randint(-500, 1500))},
               "convert": {"op": "convert", "rate": Decimal(rng.randint(950, 1050)) / 1000}}
        for _attempt in range(2 * n):
            if step_fn(replicas, replicas[(step + view) % n], txs, f, rng):
                break
            view += 1
            view_changes += 1
        else:
            stalled += 1

    honest = [r for r in replicas if not r.byzantine]
    return {
        "forked": len({tuple(r.ledger) for r in honest}) > 1,
        "view_changes": view_changes,
        "stalled": stalled,
    }


if __name__ == "__main__":
    N = 300
    configs = [
        ("naive (follow the leader)", 4, 1, "naive"),
        ("PBFT, 4 keepers, 1 crooked", 4, 1, "pbft"),
        ("PBFT, 7 keepers, 2 crooked", 7, 2, "pbft"),
        ("PBFT, 4 keepers, 2 crooked", 4, 2, "pbft"),
        ("PBFT, 6 keepers, 2 crooked", 6, 2, "pbft"),
    ]
    for label, n, b, proto in configs:
        rs = [run(s, n, b, proto) for s in range(N)]
        print(f"{label:<28} f_tolerated={(n - 1) // 3}  "
              f"runs where honest copies disagree: {sum(r['forked'] for r in rs):>3}/{N}  "
              f"avg leader swaps/run: {sum(r['view_changes'] for r in rs) / N:5.1f}  "
              f"stalled steps: {sum(r['stalled'] for r in rs)}")
