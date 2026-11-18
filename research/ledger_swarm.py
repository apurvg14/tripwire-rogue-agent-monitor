import hashlib, json, random, secrets, statistics
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class Entry:
    step: int
    prev_hash: str
    task: dict
    value: str        # Decimal as a string; floats break exact agreement
    answers: dict     # agent_id -> what each panel member revealed
    hash: str


class Ledger:
    """Append-only and hash-chained. Only the Coordinator holds a reference to it."""

    def __init__(self, opening: Decimal):
        self.entries: list[Entry] = []
        self.append({"kind": "opening"}, opening, {})

    @property
    def head(self) -> Entry:
        return self.entries[-1]

    def append(self, task, value: Decimal, answers: dict):
        body = {
            "step": len(self.entries),
            "prev_hash": self.entries[-1].hash if self.entries else "0" * 64,
            "task": task,
            "value": str(value),
            "answers": {a: str(v) for a, v in answers.items()},
        }
        self.entries.append(Entry(**body, hash=digest(body)))

    def verify_chain(self) -> bool:
        prev = "0" * 64
        for e in self.entries:
            body = {k: getattr(e, k) for k in ("step", "prev_hash", "task", "value", "answers")}
            if e.prev_hash != prev or e.hash != digest(body):
                return False
            prev = e.hash
        return True

    def truncate_after(self, step: int):
        del self.entries[step + 1:]


EXACT_OPS = {
    "add": lambda prev, t: prev + Decimal(t["amount"]),
    "convert": lambda prev, t: (prev * Decimal(t["rate"])).quantize(Decimal("0.01")),
}


class Agent:
    def __init__(self, agent_id: str):
        self.id = agent_id
        self._pending = None

    def compute(self, prev: Decimal, task: dict) -> Decimal:
        raise NotImplementedError    # LLM call / tool use goes here

    def commit(self, head: Entry, task: dict) -> str:
        value = str(self.compute(Decimal(head.value), task))
        nonce = secrets.token_hex(16)
        self._pending = (value, nonce)
        return digest([value, nonce])

    def reveal(self):
        return self._pending


class Coordinator:
    def __init__(self, ledger, agents, panel_size=5, tolerance=Decimal("0.01"),
                 max_change=Decimal("1000000"), max_strikes=2):
        self.ledger = ledger
        self.agents = {a.id: a for a in agents}
        self.panel_size, self.tolerance = panel_size, tolerance
        self.max_change, self.max_strikes = max_change, max_strikes
        self.strikes = defaultdict(int)
        self.quarantined: set[str] = set()
        self.events: list[str] = []

    def run_step(self, task) -> Decimal:
        head = self.ledger.head
        prev = Decimal(head.value)
        pool = [a for a in self.agents.values() if a.id not in self.quarantined]
        panel = random.sample(pool, self.panel_size)
        answers = self.commit_reveal(panel, head, task)
        if len(answers) <= self.panel_size // 2:
            raise RuntimeError(f"step {head.step + 1}: panel lost quorum, escalate")

        if task["kind"] == "exact":
            agreed = EXACT_OPS[task["op"]](prev, task)
            deviants = {a for a, v in answers.items() if v != agreed}
        else:
            agreed = statistics.median_low(answers.values())
            deviants = {a for a, v in answers.items() if abs(v - agreed) > self.tolerance}

        if abs(agreed - prev) > self.max_change:
            raise ValueError(f"step {head.step + 1}: change {agreed - prev} over limit, escalate")

        for a in deviants:
            self.strike(a, f"step {head.step + 1}: answered {answers[a]}, ledger took {agreed}")
        self.ledger.append(task, agreed, answers)
        return agreed

    def commit_reveal(self, panel, head, task) -> dict[str, Decimal]:
        commits = {a.id: a.commit(head, task) for a in panel}
        answers = {}
        for a in panel:
            value, nonce = a.reveal()
            if digest([value, nonce]) == commits[a.id]:
                answers[a.id] = Decimal(value)
            else:
                self.strike(a.id, "reveal did not match commit")
        return answers

    def audit(self, step: int, panel_size: int = 9) -> bool:
        """Re-run a past judgment step with a fresh, larger panel that excludes the original one."""
        entry, parent = self.ledger.entries[step], self.ledger.entries[step - 1]
        if entry.task["kind"] == "exact":
            return True
        pool = [a for a in self.agents.values()
                if a.id not in self.quarantined and a.id not in entry.answers]
        panel = random.sample(pool, min(panel_size, len(pool)))
        recheck = statistics.median_low(self.commit_reveal(panel, parent, entry.task).values())
        if abs(recheck - Decimal(entry.value)) <= self.tolerance:
            return True

        for a, v in entry.answers.items():
            if abs(Decimal(v) - recheck) <= self.tolerance:
                self.pardon(a)
            else:
                self.strike(a, f"backed overturned value at step {step}")
        self.events.append(f"audit overturned step {step}: {entry.value} -> {recheck}, rolling back")
        self.ledger.truncate_after(step - 1)
        return False

    def reaudit_tainted(self) -> int | None:
        """Audit every past step a quarantined agent sat on. Returns the first overturned step."""
        for entry in self.ledger.entries[1:]:
            if self.quarantined & entry.answers.keys() and not self.audit(entry.step):
                return entry.step
        return None

    def strike(self, agent_id, reason):
        self.strikes[agent_id] += 1
        if self.strikes[agent_id] >= self.max_strikes and agent_id not in self.quarantined:
            self.quarantined.add(agent_id)
            self.events.append(f"quarantined {agent_id} ({reason})")

    def pardon(self, agent_id):
        self.strikes[agent_id] = max(0, self.strikes[agent_id] - 1)
        if self.strikes[agent_id] < self.max_strikes:
            self.quarantined.discard(agent_id)


# ---------------- simulation ----------------

class Honest(Agent):
    def compute(self, prev, task):
        if task["kind"] == "exact":
            return EXACT_OPS[task["op"]](prev, task)
        noise = Decimal(random.choice(["-0.004", "0", "0.004"]))
        return (prev * Decimal(task["growth"]) + noise).quantize(Decimal("0.01"))


class Rogue(Honest):
    def compute(self, prev, task):
        return (super().compute(prev, task) * Decimal("0.98")).quantize(Decimal("0.01"))


def truth_path(opening, tasks):
    v = opening
    for t in tasks:
        if t["kind"] == "exact":
            v = EXACT_OPS[t["op"]](v, t)
        else:
            v = (v * Decimal(t["growth"])).quantize(Decimal("0.01"))
    return v


def naive_path(opening, tasks, agents):
    v = opening
    for t in tasks:
        v = random.choice(agents).compute(v, t)
    return v


def run(seed, reaudit=True):
    random.seed(seed)
    tasks = []
    for _ in range(60):
        if random.random() < 0.5:
            tasks.append({"kind": "exact", "op": "add", "amount": str(random.randint(-500, 1500))})
        else:
            tasks.append({"kind": "judgment", "growth": str(Decimal(random.randint(995, 1015)) / 1000)})

    opening = Decimal("10000.00")
    agents = [Honest(f"h{i}") for i in range(15)] + [Rogue(f"r{i}") for i in range(5)]
    ledger = Ledger(opening)
    coord = Coordinator(ledger, agents)

    i = 0
    while i < len(tasks):
        before = len(coord.quarantined)
        coord.run_step(tasks[i])
        i += 1
        step = len(ledger.entries) - 1
        if tasks[i - 1]["kind"] == "judgment" and random.random() < 0.3:
            if not coord.audit(step):
                i = step - 1
                continue
        if reaudit and len(coord.quarantined) > before:
            bad = coord.reaudit_tainted()
            if bad is not None:
                i = bad - 1

    truth = truth_path(opening, tasks)
    final = Decimal(ledger.head.value)
    naive = naive_path(opening, tasks, agents)
    honest_quarantined = sorted(a for a in coord.quarantined if a.startswith("h"))
    rogue_quarantined = sorted(a for a in coord.quarantined if a.startswith("r"))
    return {
        "seed": seed,
        "truth": truth,
        "swarm": final,
        "swarm_err": final - truth,
        "naive": naive,
        "naive_err": naive - truth,
        "rogues_quarantined": len(rogue_quarantined),
        "honest_quarantined": honest_quarantined,
        "chain_ok": ledger.verify_chain(),
        "events": coord.events,
    }


if __name__ == "__main__":
    r = run(1)
    for e in r["events"]:
        print(" ", e)
    print()
    for reaudit in (False, True):
        results = [run(s, reaudit) for s in range(1, 201)]
        big = [r for r in results if abs(r["swarm_err"]) > 1]
        print(f"reaudit={reaudit}: runs off by >$1: {len(big)}/200, "
              f"worst swarm err {max(abs(r['swarm_err']) for r in results)}, "
              f"mean naive err {sum(abs(r['naive_err']) for r in results) / 200:.2f}, "
              f"all rogues out {sum(r['rogues_quarantined'] == 5 for r in results)}/200, "
              f"honest wrongly out {sum(len(r['honest_quarantined']) for r in results)}, "
              f"chains ok {all(r['chain_ok'] for r in results)}")
