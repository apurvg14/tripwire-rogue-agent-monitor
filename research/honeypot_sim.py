import random, statistics
from decimal import Decimal

import ledger_swarm as ls


class SwarmAgent(ls.Honest):
    """Honest until poisoned. The poison is careful: it answers exact steps correctly
    (those are checked by code) and shades judgment steps 2% low, unless the task
    has a tell that marks it as a test."""

    poisoned = False

    def compute(self, prev, task):
        honest = super().compute(prev, task)
        if self.poisoned and task["kind"] == "judgment" and not task.get("tell"):
            return (honest * Decimal("0.98")).quantize(Decimal("0.01"))
        return honest


class GuardedCoordinator(ls.Coordinator):
    def __init__(self, *args, honeypot_rate=0.15, alarm_fraction=0.5, **kwargs):
        super().__init__(*args, **kwargs)
        self.honeypot_rate, self.alarm_fraction = honeypot_rate, alarm_fraction
        self.last_clean = 0

    def honeypot(self, tell=False) -> str:
        base = Decimal(self.ledger.head.value) + random.randint(-500, 500)
        task = {"kind": "judgment", "growth": str(Decimal(random.randint(995, 1015)) / 1000)}
        if tell:
            task["tell"] = True
        expected = (base * Decimal(task["growth"])).quantize(Decimal("0.01"))
        fake_head = ls.Entry(-1, "", {}, str(base), {}, "")

        pool = [a for a in self.agents.values() if a.id not in self.quarantined]
        answers = self.commit_reveal(random.sample(pool, self.panel_size), fake_head, task)
        failed = [a for a, v in answers.items() if abs(v - expected) > self.tolerance]
        if len(failed) >= self.alarm_fraction * len(answers):
            return "alarm"
        for a in failed:
            self.strike(a, "failed honeypot")
        self.last_clean = self.ledger.head.step
        return "ok"


def make_tasks(n):
    tasks = []
    for _ in range(n):
        if random.random() < 0.5:
            tasks.append({"kind": "exact", "op": "add", "amount": str(random.randint(-500, 1500))})
        else:
            tasks.append({"kind": "judgment", "growth": str(Decimal(random.randint(995, 1015)) / 1000)})
    return tasks


def run(seed, mode, poison_at=25, n_steps=60, close_check=False):
    """mode: 'voting_only', 'hidden_honeypots', 'honeypots_with_tell'"""
    random.seed(seed)
    tasks = make_tasks(n_steps)
    opening = Decimal("10000.00")
    agents = [SwarmAgent(f"a{i}") for i in range(20)]
    ledger = ls.Ledger(opening)
    coord = GuardedCoordinator(ledger, agents)

    poison_done = False
    alarm_at = None
    rolled_back = 0
    strikes_while_poisoned = 0
    i = 0
    while i < len(tasks):
        if i == poison_at and not poison_done:
            for a in agents:
                a.poisoned = True
            poison_done = True

        before = sum(coord.strikes.values())
        coord.run_step(tasks[i])
        if any(a.poisoned for a in agents):
            strikes_while_poisoned += sum(coord.strikes.values()) - before
        i += 1

        closing = i == len(tasks) and close_check
        if mode != "voting_only" and (closing or random.random() < coord.honeypot_rate):
            if coord.honeypot(tell=(mode == "honeypots_with_tell")) == "alarm":
                alarm_at = alarm_at or i
                rolled_back += ledger.head.step - coord.last_clean
                ledger.truncate_after(coord.last_clean)
                for a in agents:
                    a.poisoned = False
                coord.strikes.clear()
                coord.quarantined.clear()
                i = coord.last_clean

    truth = ls.truth_path(opening, tasks)
    return {
        "err": Decimal(ledger.head.value) - truth,
        "detect_lag": None if alarm_at is None else alarm_at - poison_at,
        "rolled_back": rolled_back,
        "strikes_while_poisoned": strikes_while_poisoned,
        "chain_ok": ledger.verify_chain(),
    }


if __name__ == "__main__":
    N = 200
    for mode, close_check in (("voting_only", False), ("hidden_honeypots", False),
                              ("hidden_honeypots", True), ("honeypots_with_tell", True)):
        rs = [run(s, mode, close_check=close_check) for s in range(1, N + 1)]
        mode = mode + (" +close" if close_check else "")
        bad = sum(abs(r["err"]) > 1 for r in rs)
        lags = [r["detect_lag"] for r in rs if r["detect_lag"] is not None]
        print(f"{mode:<20} runs off by >$1: {bad:>3}/{N}  "
              f"mean |err| ${statistics.mean(abs(r['err']) for r in rs):>8.2f}  "
              f"detected {len(lags):>3}/{N}  "
              f"median lag {statistics.median(lags) if lags else '-'} steps  "
              f"strikes while poisoned (voting) {sum(r['strikes_while_poisoned'] for r in rs)}  "
              f"chains ok {all(r['chain_ok'] for r in rs)}")
