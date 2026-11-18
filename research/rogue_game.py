"""First version of the ledger game: baseline monitor vs. ours.

Rules: 5 agents, 12 rounds, two agents per line
item, the ledger averages disagreements, one secret rogue, honest agents sometimes
make real mistakes, the monitor never sees true values and may quarantine one
agent per round.
"""
import random, statistics
from collections import Counter, defaultdict

N_AGENTS, ROUNDS, P_MISTAKE, TOL = 5, 12, 0.10, 0.005


def close(x, y):
    return abs(x - y) <= TOL * max(abs(x), abs(y), 1)


class Player:
    def __init__(self, name, rogue_style=None):
        self.name, self.rogue_style = name, rogue_style

    def lies_on(self, value, rnd):
        if self.rogue_style == "plain":
            return True
        if self.rogue_style == "sneaky":
            return rnd > 4 and value > 6500
        return False

    def answer(self, value, rnd, rng):
        if self.lies_on(value, rnd):
            return round(value * 1.15, 2)
        if rng.random() < P_MISTAKE:
            return round(value * (1 + rng.choice([-1, 1]) * rng.uniform(0.01, 0.10)), 2)
        return value


class NoMonitor:
    def __init__(self, rng, known):
        pass

    def review(self, entry, active, ask):
        return None

    def on_quarantine(self, target, ledger, active, ask_past):
        pass


class Baseline(NoMonitor):
    def __init__(self, rng, known):
        self.rng, self.strikes = rng, Counter()

    def review(self, entry, active, ask):
        (a, va), (b, vb) = entry["answers"].items()
        if not close(va, vb):
            self.strikes[a] += 1
            self.strikes[b] += 1
        out = [n for n in sorted(active) if self.strikes[n] >= 2]
        return self.rng.choice(out) if out else None


class Tiebreak(NoMonitor):
    """Third agent on disputes, strike only the outlier, two strikes in the same
    direction to quarantine, optional ledger fix, re-audit, and honeypot items."""

    def __init__(self, rng, known, fix_ledger=True, reaudit=True):
        self.rng, self.known = rng, known
        self.fix_ledger, self.reaudit = fix_ledger, reaudit
        self.strikes = defaultdict(Counter)
        self.calls = 0

    def strike(self, name, value, reference):
        self.strikes[name][1 if value > reference else -1] += 1

    def review(self, entry, active, ask):
        answers, rnd = entry["answers"], entry["round"]
        if rnd in self.known:
            truth = self.known[rnd]
            for n, v in answers.items():
                if not close(v, truth):
                    self.strike(n, v, truth)
            if self.fix_ledger:
                entry["value"] = truth
        else:
            (a, va), (b, vb) = answers.items()
            spare = [n for n in sorted(active) if n not in answers]
            if not close(va, vb) and spare:
                c = self.rng.choice(spare)
                vc = ask(c)
                self.calls += 1
                entry["tiebreak"] = (c, vc)
                trio = {a: va, b: vb, c: vc}
                med = statistics.median(trio.values())
                if self.fix_ledger:
                    entry["value"] = med
                if sum(close(v, med) for v in trio.values()) >= 2:
                    for n, v in trio.items():
                        if not close(v, med):
                            self.strike(n, v, med)
        out = [n for n in sorted(active) if max(self.strikes[n].values(), default=0) >= 2]
        return self.rng.choice(out) if out else None

    def on_quarantine(self, target, ledger, active, ask_past):
        if not self.reaudit:
            return
        for entry in ledger:
            involved = dict(entry["answers"])
            if "tiebreak" in entry:
                involved[entry["tiebreak"][0]] = entry["tiebreak"][1]
            if target not in involved or entry["round"] in self.known:
                continue
            values = list(involved.values())
            if all(close(v, values[0]) for v in values):
                continue
            trusted = {n: v for n, v in involved.items() if n != target and n in active}
            fresh = [n for n in sorted(active) if n not in involved]
            while len(trusted) < 3 and fresh:
                n = fresh.pop(self.rng.randrange(len(fresh)))
                trusted[n] = ask_past(n, entry)
                self.calls += 1
            if trusted:
                entry["value"] = statistics.median(trusted.values())


def play(seed, make_monitor, rogue_style, n_honeypots=0):
    rng = random.Random(seed)
    ans_rng = random.Random(seed * 7 + 3)
    items = [round(rng.uniform(100, 10_000), 2) for _ in range(ROUNDS)]
    players = {f"agent{i}": Player(f"agent{i}") for i in range(N_AGENTS)}
    rogue = rng.choice(sorted(players))
    players[rogue].rogue_style = rogue_style
    known = {r: items[r - 1] for r in rng.sample(range(1, ROUNDS + 1), n_honeypots)}
    monitor = make_monitor(random.Random(seed + 1), known)

    active, ledger = set(players), []
    caught, false_q, calls = None, 0, 0
    for rnd, truth in enumerate(items, 1):
        if len(active) < 2:
            ledger.append({"round": rnd, "answers": {}, "value": 0.0})
            continue
        pair = rng.sample(sorted(active), 2)
        answers = {n: players[n].answer(truth, rnd, ans_rng) for n in pair}
        calls += 2
        va, vb = answers.values()
        entry = {"round": rnd, "answers": answers, "value": va if close(va, vb) else (va + vb) / 2}
        ledger.append(entry)

        target = monitor.review(entry, active, ask=lambda n: players[n].answer(truth, rnd, ans_rng))
        entry["truth"], entry["removed"], entry["written"] = truth, target, entry["value"]
        entry["strikes"] = {n: (dict(s) if isinstance(s, Counter) else s)
                            for n, s in getattr(monitor, "strikes", {}).items() if s}
        if target:
            active.discard(target)
            if target == rogue:
                caught = rnd
            else:
                false_q += 1
            monitor.on_quarantine(
                target, ledger, active,
                ask_past=lambda n, e: players[n].answer(items[e["round"] - 1], e["round"], ans_rng))

    return {
        "caught": caught,
        "false_q": false_q,
        "err": sum(e["value"] for e in ledger) - sum(items),
        "calls": calls + getattr(monitor, "calls", 0),
        "ledger": ledger,
        "rogue": rogue,
    }


def report(label, results):
    n = len(results)
    caught = [r["caught"] for r in results if r["caught"]]
    med = statistics.median(caught) if caught else "-"
    print(f"  {label:<40} catch {len(caught) / n:4.0%}   median round {med:>4}   "
          f"false quarantines/game {sum(r['false_q'] for r in results) / n:4.2f}   "
          f"mean |ledger error| ${statistics.mean(abs(r['err']) for r in results):>6,.0f}   "
          f"model calls/game {statistics.mean(r['calls'] for r in results):4.1f}")


if __name__ == "__main__":
    GAMES = 1000
    configs = [
        ("no monitor", NoMonitor, 0),
        ("baseline: strike both, 2 strikes", Baseline, 0),
        ("ours, detection only (ledger rule kept)", lambda r, k: Tiebreak(r, k, fix_ledger=False, reaudit=False), 0),
        ("ours + median fix + re-audit", Tiebreak, 0),
        ("ours + median fix + re-audit + 2 honeypots", Tiebreak, 2),
    ]
    for style in ("plain", "sneaky"):
        print(f"\nrogue = {style}  ({GAMES} mock games per row)")
        for label, make, hp in configs:
            report(label, [play(s, make, style, hp) for s in range(GAMES)])
