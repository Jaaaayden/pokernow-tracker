# Privacy

Tracker for PokerNow keeps a record of the poker hands you play on PokerNow, on your
own computer. This page says what that record holds, where it goes, and what each
of the extension's permissions is for. Tracker for PokerNow is not affiliated with
PokerNow.

## What it stores

- **The logs of the games you open.** Every line PokerNow's own game log shows at
  the table: seats, actions, showdowns, stacks, and the names and PokerNow IDs of
  the players. Where PokerNow sends it to you, this includes your own hole cards.
- **What you add yourself:** which of a player's IDs are the same person (aliases),
  notes on hands, and marks on the hands you have reviewed.
- **Your settings.**

Everything shown (stats, charts, reviews) is worked out from those logs on your
computer.

## Where it is kept

In the SQLite file `pnt setup` created on your computer (`pnt where` names it), plus
a CSV copy of each game in `~/Downloads/pokernow-logs`. The local server listens
only on your own computer (127.0.0.1), answers only requests that name this machine
as their host, and takes writes only from the extension and its own pages.

Your settings are kept with Chrome's synced storage, so they follow your Chrome
account like any extension's settings. Nothing else is synced.

## What it sends, and to whom

- **To PokerNow:** requests for the log of a game you have open, or one whose link
  you gave `pnt backfill`. They go with your PokerNow login, exactly as the game
  page itself would ask.
- **To the local server:** the same logs, over your own computer's loopback
  address.
- **To anyone else: nothing.** No analytics, no crash reports, no accounts, no
  remote server of ours.

The data is not sold, shared, or used for anything but showing it back to you.

## Permissions, and why

| Permission | What it is used for |
|---|---|
| `pokernow.com/games/*`, `pokernow.club/games/*` | Read the game log of the table you are at, and recognise game tabs so the side panel can open on them |
| `127.0.0.1`, `localhost` | Send the log to the local server and read the HUD's figures back |
| `storage` | Settings |
| `sidePanel` | Show the HUD beside the game, not over it |

## Deleting it

Delete the database file (`pnt where` names it) and the log folder, run
`pnt service uninstall`, and remove the extension.

## Questions

Open an issue at <https://github.com/Jaaaayden/pokernow-tracker/issues>.
