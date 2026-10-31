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
  notes on hands and players, marks on the hands you have reviewed, 🚩 flags, hand
  tags and saved spots.
- **Your settings:** the extension's (server address, poll interval, side panel or
  floating), the HUD's chosen stats, and which database and log folder to use.

Everything shown (stats, charts, reviews) is worked out from those logs on your
computer.

## Where it is kept

In the SQLite file `pnt setup` created on your computer (`pnt where` names it), plus
a CSV copy of each game in your log folder (`~/Downloads/pokernow-logs` unless you
chose another). Which database and folder you chose is kept in `~/.pnt/config.json`,
and the HUD's chosen stats in the database. The local server listens
only on your own computer (127.0.0.1), answers only requests that name this machine
as their host, and takes writes only from the extension and its own pages.

The extension's own settings are kept with Chrome's synced storage, so they follow
your Chrome account like any extension's settings. Nothing else is synced.

## What it sends, and to whom

- **To PokerNow:** requests for the log of a game you have open, or one whose link
  you gave `pnt backfill`. They go with your PokerNow login, exactly as the game
  page itself would ask.
- **To the local server:** the same logs, over your own computer's loopback
  address.
- **To GitHub, to check for updates:** an install from a GitHub archive (the one
  the README gives) has its local server ask GitHub's API whether a newer version
  exists, when it starts and then every 6 hours:
  `GET https://api.github.com/repos/Jaaaayden/pokernow-tracker/compare/<commit>...<branch>`,
  where `<commit>` is the version you installed and `<branch>` the branch it came
  from, with the user agent `pnt`. GitHub sees your IP address, as with any web
  request. Nothing else is sent: no hands, names, stats, settings or login. Set
  `PNT_UPDATE_CHECK=0` in the server's environment to turn it off. A copy run from
  a source checkout, or installed with `git+`, never checks. `pnt update`
  downloads the new version from GitHub, and only when you run it.
- **To anyone else: nothing.** No analytics, no crash reports, no accounts, no
  remote server of ours. The extension itself talks only to PokerNow and the
  local server.

The data is not sold, shared, or used for anything but showing it back to you.

## Permissions, and why

| Permission | What it is used for |
|---|---|
| `pokernow.com/games/*`, `pokernow.club/games/*` | Read the game log of the table you are at, and recognise game tabs so the side panel can open on them |
| `127.0.0.1`, `localhost` | Send the log to the local server and read the HUD's figures back |
| `storage` | Settings |
| `sidePanel` | Show the HUD beside the game, not over it |
| `scripting` | When the extension is installed or updated, re-attach its own log reader to PokerNow game tabs already open, so they keep recording without a reload. It runs only the extension's own files, only on those tabs |

## Deleting it

Delete the database file (`pnt where` names it), the log folder and `~/.pnt`, run
`pnt service uninstall`, and remove the extension.

## Questions

Open an issue at <https://github.com/Jaaaayden/pokernow-tracker/issues>.
