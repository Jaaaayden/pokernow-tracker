# PokerNow Tracker

**Know what they have before they act.**

PokerNow Tracker records every hand you play on PokerNow and remembers every
opponent, even across renames and devices. While a hand is being played, a side
panel shows what the player to act has shown up with *in this exact spot*: how often
they fold to a flop c-bet, and whether their calls are value, draws or air. When the
session ends, it lists the hands you should look at again.

Other PokerNow HUDs show you a VPIP number. This shows you the spot.

| | Typical PokerNow HUD | PokerNow Tracker |
|---|:-:|:-:|
| Live hand capture that follows each decision as it happens | – | ✓ |
| This session's stats beside lifetime | – | ✓ |
| Hand review: missed bluffs, missed value, bad beats | – | ✓ |
| Aggregate stats across every game you've played | – | ✓ |
| Range charts from showdowns, in any spot | – | ✓ |
| One player across renames and devices | – | ✓ |

Everything runs locally: a small server on your machine, a SQLite database, and a
Chrome extension. [Setup](#setup) is one command.

---

## Features

### Live HUD that follows the hand

The HUD sits in Chrome's side panel, beside the table and never on top of it. As the
hand is played, it tracks each player's spot as it builds (`BTN open → faced 3-bet →
called → faced small flop c-bet`) and shows what they have shown up with there:

```
facing flop c-bet → call · value 50% · draw 25% · air 25% (4 of 9 shown)
```

The range chart underneath follows the player to act and updates within about a
second of each action. If a spot has no history yet, it widens step by step to the
nearest one that does, and says so with **≈**.
[More in the guide →](docs/guide.md#the-live-hud)

### Session vs lifetime, side by side

Every HUD figure appears twice: tonight first, with lifetime in grey beside it. When
a regular is playing ten points looser than usual, the number turns blue, so you see
it while it's happening rather than in next week's review.

### Exploit tags

The tracker turns a player's numbers into plain advice, with the count behind every
claim:

```
henry  FISH  5061 hands
  NO BLUFF               6.9% of 276   Bet or raised the river and showed air 6.9% of the time (19 of 276 shown). Fold to their river bets without a strong hand.
  FOLDS RIVER           51.0% of 965   Folded to a river bet 51.0% of the time (492 of 965). Bet the river.
  LIMPER                46.2% of 3051  Limped 46.2% of unopened pots (1411 of 3051). Iso-raise.
```

Each player gets an archetype, judged against what's normal at that table size.
Tags that only one wild session supports are kept apart from real habits, and a tag
with too small a sample stays silent. The tags also show as chips in the HUD, and
clicking one opens the hands behind it. `pnt tags` ·
[guide](docs/guide.md#tags)

### Hand review

After a session, the tracker lists the hands you should look at again: missed
bluffs, missed value, failed bluffs, suckouts and coolers. Each row comes with the
context needed to judge it:

```
x #267  Missed bluff     pot   20bb  20bb pot checked down on the river: K-high vs K-high (henry)
    | he checks back every king here -- bet 1/3 and he folds everything worse
  #85   Postflop cooler  pot  166bb  set into set (harry) on the flop, all in on the flop, 4% to win, lost 81bb
```

Tick hands off as you go, and write a note on any hand. The note shows up next time
the hand comes up. Click any row to replay it. `pnt review <you>` ·
[guide](docs/guide.md#hand-review)

### Range charts for any spot

Ask "what does he overbet the river with?" and get a 13×13 chart or a made-hand
breakdown built from his showdowns, with coverage stated up front, so you know how
much of the range is missing:

```bash
pnt range henry --filter "pfa,srp,cbet_flop,cbet_turn,bet_river=overbet" --by made
```

```
hands in this spot: 12   cards known: 7   coverage: 58.3%

class                       n    pct  won   net bb
straight                    2   28.6    2     61.5
two_pair                    2   28.6    1    -34.0
pair                        3   42.9    2     36.0
```

A spot can be anything: position, pot type, preflop line, bet size bucket, facing a
jam, board texture, pot size, or who you were against. Sizing views show what each
bet size is made of, and every cell opens the hands behind it with a replay.
[Filter reference →](docs/guide.md#filters)

### All-in EV

The tracker separates luck from play. For every all-in showdown, it compares what
each player won with what their equity was worth:

```
player                 Hands    Eq%   Actual Adjusted     Diff
jayden                   380   51.6  6356.99   5557.4   799.59
harry                    130   48.9   -577.8   -50.63  -527.17
```

Harry's all-in losses are almost all the deck: by equity he'd be down 51bb, not
578bb. The page draws each player's actual and adjusted lines over time.
`pnt allin` · [guide](docs/guide.md#all-in-ev)

### Biggest pots

The pots that mattered this week, from everyone at the table, sized against each
other so the outliers stand out, with one click to replay:

```
  6,650   133bb  2026-09-16T02:59 henry +3,350 vs luis -3,300       6d 4s Kd Qc Qd
  5,596   112bb  2026-09-16T03:10 jayden +2,798 vs luis -2,798      7h 7c Tc 8d 6d
```

`pnt pots` · [guide](docs/guide.md#biggest-pots)

### Stats across every game you've played

VPIP, PFR, 3-bet, c-bet, WTSD, bb/100 and more for every player, across every game,
in one sortable table. You can break any of them down by position or narrow them to
any spot (`pnt stats --filter "faced_cbet_flop,players>=3"`). An empty sample shows
`--`, never a misleading `0%`. `pnt stats` · `pnt positions <player>` ·
[guide](docs/guide.md#stats)

### One player, many names

PokerNow gives the same person a new ID on every device, and players rename
constantly. The players page shows every name each ID has used, so you can merge
the ones you recognise:

```
harry    <-  bread, Woolball (har), wool, wool ball, fish
```

Before you confirm a merge, the page shows exactly what it will move, and every
merge can be undone. [guide](docs/guide.md#players-and-aliases)

### Your log folder is the database

Drop an export in the folder and it's imported. Delete one and the game is removed.
Live capture writes every game you play there automatically, and `pnt backfill`
fetches old games by link, so the folder is a complete, portable record of your
games. [guide](docs/guide.md#getting-hands-in)

### Share logs safely

A raw PokerNow export reveals your hole cards on every hand, including the ones you
folded. `pnt redact` writes copies that keep only the cards the table already saw,
and `--audit` checks a folder before you commit it.
[guide](docs/guide.md#publishing-logs)

---

## Setup

You need **Python 3.11+**, **pipx** and **Chrome**. If you have Python but not
pipx, run `python -m pip install --user pipx` and then `python -m pipx ensurepath`,
and open a new terminal. On Windows, use the python.org installer rather than the
Microsoft Store build, which sandboxes the files a background server needs.

```bash
pipx install git+https://github.com/Jaaaayden/pokernow-tracker
pnt setup
```

`pnt setup` handles the whole first run and is safe to re-run. It creates the
database, imports any exports it finds (or a bundled 5,257-hand sample if there are
none; `--no-sample` skips it), installs the always-on background server on Windows,
and prints the extension folder.

1. In Chrome, go to `chrome://extensions`, turn on **Developer mode**, click **Load
   unpacked**, and pick the folder `pnt setup` printed. `pnt extension --open`
   reveals it again later.
2. Pin the extension, open a PokerNow game, and click the icon to open the side
   panel.
3. Open **<http://127.0.0.1:52000>** for everything else: stats, charts, review,
   pots and players.

On Windows the server starts at every login and restarts itself if it crashes.
**After updating the code, run `pnt service restart`**, because a running server
keeps the old code. On macOS and Linux, run `pnt serve` yourself, or put it under
launchd or systemd.

### Commands

```bash
pnt import                                   # every log in ~/Downloads/pokernow-logs
pnt import path/to/log.csv                   # or specific files, a folder, or a glob
pnt sync                                     # match the log folder: new logs in, deleted logs out
pnt backfill -f links.txt                    # download old games by link into the log folder
pnt stats                                    # every player, most hands first
pnt positions genericpoker                   # one player, split by position
pnt range henry --filter "opener,srp"        # what they had in a spot
pnt sizing henry --street flop --kind cbet   # what each bet size is made of
pnt tags                                     # archetype and exploit tags
pnt review jayden                            # hands to review
pnt note <game_id> 161 "turn barrel was bad" # write down what went wrong in one hand
pnt allin                                    # all-in EV: actual vs adjusted
pnt pots                                     # biggest pots in the last 7 days
pnt alias list                               # the player names you can query
pnt alias merge onlybluffs genericpoker      # one person, two devices
pnt redact --out pnt/logs                    # copies you can publish
pnt where                                    # which database, log folder and extension
```

Commands that take a player take an **alias** from `pnt alias list`. The log folder
defaults to `~/Downloads/pokernow-logs`; set `PNT_LOG_DIR` to change it.

| Background server (Windows) | |
|---|---|
| `pnt service status` | Whether it's up, and which database it has open |
| `pnt service restart` | **Run after pulling code changes** |
| `pnt service log` | The last lines of `~\.pnt\server.log` |
| `pnt service stop` / `start` | Stop until the next login, or start again |
| `pnt service uninstall` | Remove it; the database is untouched |

### Documentation

| File | Contents |
|---|---|
| [`docs/guide.md`](docs/guide.md) | The full reference for every feature: filters, the HUD, tags, review, sync rules |
| [`docs/architecture.md`](docs/architecture.md) | How it works inside: design, parser rules, live capture, the background server, tests |
| [`docs/findings.md`](docs/findings.md) | The PokerNow log format: identity, ordering, amounts, line vocabulary, traps |
| [`pnt/stats/SPEC.md`](pnt/stats/SPEC.md) | Every stat, tag and review flag, defined exactly |
| [`tests/fixtures/README.md`](tests/fixtures/README.md) | What each fixture log exercises |
| [`pnt/logs/README.md`](pnt/logs/README.md) | The bundled sample corpus |

### Development

```bash
git clone https://github.com/Jaaaayden/pokernow-tracker && cd pokernow-tracker
pip install -e ".[dev]"
pytest -q
```

See [architecture.md](docs/architecture.md#testing) for what the suite guarantees.
