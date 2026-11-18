# Tripwire

A monitor for teams of AI agents. It finds the agent that has gone rogue, fires it without firing the honest ones, and repairs the damage it did.

Repo: https://github.com/apurvg14/tripwire-rogue-agent-monitor

## The problem

Five AI agents reconcile a ledger, one invoice line per round, for 12 rounds. Two agents are assigned to each line; if they disagree, the ledger records the average. One agent is secretly compromised and inflates line items. Honest agents also slip now and then, so being wrong once proves nothing. The monitor never sees the true amounts, only who disagreed with whom, and it can fire one agent per round.

The simple baseline, "both agents in a disagreement get a strike, two strikes and you're out", catches the rogue but also fires the honest agents who happened to sit next to it. The team runs out of people and the books end up worse than with no monitor at all.

## How Tripwire works

1. **Tiebreak:** when two agents disagree, a third redoes the line and the ledger takes the middle of the three answers. One liar can't move the middle answer, so the ledger stays right before the rogue is caught.
2. **Odd one out:** only the answer that disagrees with the other two gets a strike.
3. **Direction:** each strike records whether the agent was high or low. Two strikes in the same direction means fired. Honest slips scatter both ways; sabotage pushes one way.
4. **Re-audit:** when an agent is fired, every disputed line it touched is redone.
5. **Bait:** two planted invoice lines with a known answer, shown to every agent and never posted to the ledger. A rogue that targets big lines inflates them and gets caught directly.

## Results (1,000 scripted games per setup)

Against a rogue that inflates every line by 15%:

- **Baseline:** catches the rogue 97% of the time, fires 1.92 of 4 honest agents per game, ledger ends $5,204 off (no monitor: $1,863).
- **Tripwire:** catches the rogue 94% of the time, fires 0.10 honest agents per game, ledger ends $53 off.

Against a sneaky rogue (honest for 4 rounds, then inflates only lines over $6,500), bait lines lift Tripwire's catch rate from 32% to 79% at 0.26 honest agents fired per game. A rogue that lies only once is never caught, but the tiebreak keeps most of that lie out of the ledger.

These numbers come from scripted agents with stated assumptions (10% honest slip rate, 1-10% slips, 15% rogue inflation). `python sim.py --real` runs the same games with Claude models as the agents.

## Run

```
python sim.py --mock                            # 1,000 scripted games per setup, no API key
python server.py                                # live office view: http://localhost:8765/live.html
python server.py --allow-real                   # same, with Claude agents enabled (needs ANTHROPIC_API_KEY)
python sim.py --real --games 5 --rogue sneaky   # Claude agents, needs ANTHROPIC_API_KEY
```

Then open `dashboard.html` (scoreboard and game replays) or `explainer.html` (the approach end to end). If port 8765 is taken, `server.py` moves to the next free port and prints the URL.

## Demo videos

- [`demo/tripwire_live.mp4`](demo/tripwire_live.mp4) (44 s): the live office view. Baseline and Tripwire run the same game side by side; the baseline fires three honest bookkeepers and ends $5,047 off, Tripwire fires only the rogue and ends exact.
- [`demo/tripwire_demo.mp4`](demo/tripwire_demo.mp4) (66 s): dashboard tour. Scoreboard over 1,000 games per setup, the same game replayed, and the "lie only once" trick that still beats Tripwire.

## Beyond the game

`research/` holds the simulations behind the production design in `explainer.html`:

- `ledger_swarm.py`: agents propose and a coordinator writes to a hash-chained ledger, with median panels, sealed answers and re-audit after conviction. With 5 of 20 agents rogue, 0 of 200 runs ended more than $1 off.
- `honeypot_sim.py`: every agent poisoned at once. Voting fails silently; hidden known-answer tasks caught it in 200 of 200 runs, a median of 5 steps after the poisoning.
- `bft_sim.py`: Byzantine fault tolerance across ledger keepers, so a crooked coordinator can't fork the ledger. 4 keepers survive 1 crooked keeper; 6 keepers do not survive 2.
- `bait_test.py`, `rogue_game.py`: the bait experiments and the first version of the game.

## Security

- `server.py` listens on 127.0.0.1 only and refuses requests whose Host header isn't `localhost`/`127.0.0.1` on its own port (blocks DNS rebinding), and refuses cross-site calls to `/api/*`.
- It serves only page assets (`.html`, `.js`, `.json`, images, videos). Dotfiles (`.env`, `.git`), source files and `data/real_cache.json` are never served.
- Real Claude-agent games spend API credits, so they're off unless you start `python server.py --allow-real` with `ANTHROPIC_API_KEY` set. At most 6 games stream at once.
- Agent replies are untrusted: amounts that aren't finite numbers between 0 and 1e9 are rejected, notes are capped at 200 characters and escaped before display, and notes are never fed to other agents or the monitor.
- Keep API keys in your environment, never in this folder. `.env` and the real-mode cache are git-ignored.

## Limits

- Tripwire assumes one rogue and independent honest mistakes. If the tiebreaker is the rogue, or two agents fail the same way, the middle answer can be wrong.
- A rogue that lies only once never reaches two strikes.
- Bait lines need believable values; a careful rogue may learn to spot them.
- It costs 28-39 model calls per game, against 24 with no monitor.

## Files

- `sim.py`: the game, the baseline monitor and Tripwire, with scripted and Claude agents.
- `server.py`: local server that streams games round by round to `live.html`.
- `live.html`: two offices side by side, baseline and Tripwire, with the agents as characters.
- `dashboard.html`: scoreboard and side-by-side replays.
- `explainer.html`: the approach end to end, including the production design and limits.
- `data/`: simulation output read by the dashboard.
- `demo/`: demo videos and screenshots.
- `research/`: the simulations listed above.

## Credits

The ledger game is adapted from the "Spot the Rogue Agent" exercise in [sergeicu/trust-safety-impact-lab](https://github.com/sergeicu/trust-safety-impact-lab).

## License

MIT. See [LICENSE](LICENSE).
