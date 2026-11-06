- Project: https://github.com/linuxcaffe/tw-gamify
- Issues:  https://github.com/linuxcaffe/tw-gamify/issues

# tw-gamify

Taskwarrior: The Game. Every task action earns or costs points, and so does every task you ignore. Spend the points on prizes you choose, under the supervision of a disappointed grandmother.

## TL;DR

```
$ task 12 done
⚡ +24 🪙  Early! I'll tell everyone at church.
$ task 7 modify due:friday
💃 −2 🪙  Moved it again. No, no, I'm not upset. I'm only asking.
$ task next
🧾 The Reckoning: −3 🪙  🍂 rot×2 −6 · 🏆 clean +3
   Some good, some not. I've kept a note of both.
```

- Scores what you do as you do it, and judges what you didn't do once a day
- A finished task pays `points × urgency × punctuality`; late always beats never, and deleting never beats doing
- XP sets your rank and never drops; coins buy prizes and can go into debt
- Everything is adjustable in `~/.task/config/gamify.rc`
- Works on every client: its remarks are hook feedback, which tw-web already shows
- Follows you between stations: gittw merges the ledger
- Requires Taskwarrior 2.6.x and Python 3.7+

## Install

```bash
tw -I gamify          # from the registry, or:
bash gamify.install   # from this folder
```

The game starts with your next task action. Tasks you already have get an amnesty of 14 days (`gamify.amnesty-days`).

## Commands

| Command | |
|---|---|
| `gamify` | The status board |
| `gamify log [N]` | The ledger: what you got and why |
| `gamify shop` / `gamify redeem <key>` | The prizes, and buying one |
| `gamify pause [DAYS]` / `gamify resume` | No penalties for a while |
| `gamify -e` | Edit the settings |
| `gamify --line` | One line, for a prompt or status bar |
| `gamify --json` | Everything, for a client |

## What scores

Default values; each one is a setting.

| When | Coins |
|---|---|
| Finished a task | 10 × urgency factor (1 to 3) × punctuality |
| ...early / on the due day / late | × 1.5 / × 1.25 / × 0.1 less per day, no lower than 0.25 |
| ...and started within the last day | +3 |
| ...and it was your most urgent task | +15 |
| ...within 5 minutes of adding it | Just +2: no farming |
| `task log` | +2 |
| Added with a project and a date | +1 (10 a day) |
| Gave a task its missing project, due or priority | +1 (5 a day) |
| Deleted a task over 60 days old | +2 |
| Deleted an overdue task | −5 |
| Pushed a due date later | −1, −2, −4, −8: doubling per task |
| `task undo`, or reopening a task | The points go back |

Judged once a day, for the days that have finished:

| What | Coins |
|---|---|
| 🍂 Overdue | −2 × urgency factor, per task per day, 30 charges at most |
| 🐢 Past its scheduled date | −1, per task per day |
| 🧟 Started and still active | −2, per task per day |
| 🦕 Untouched for 90 days, with no future date | −1, per task per week |
| 💤 Finished nothing all day | −5 |
| 🏆 Nothing overdue or behind | +5 |

One day's task penalties cost 25 at most. Blocked and waiting tasks aren't judged. Nothing is judged on a public holiday from tw-events or during a pause, and streaks survive those days. Late tasks pay double at a full moon.

Tag a task `+noscore` and it doesn't play. Automation doesn't score either: `task import`, and tasks written by another hook.

## Make it adjustable

`gamify.rc` lists every setting, with its default. A few favourites:

```
gamify.voice  = grandmother     # coach | sergeant | commentator
gamify.volume = normal          # off | quiet | normal | loud

gamify.prize.pie.cost  = 75
gamify.prize.pie.label = 🥧 The good pie

gamify.rank.0   = Peasant       # any rank replaces the built-in twelve
gamify.rank.500 = Duke

gamify.say.grandmother.push.1 = Again? I've outlived three of your deadlines.

gamify.rule.no-project.filter  = +PENDING project:   # your own rules
gamify.rule.no-project.points  = -1
gamify.rule.no-project.cadence = weekly
```

`gamify.enabled = no` pauses everything and keeps the ledger. A setting in `~/.task/local.rc` applies to that station and wins.

## How it works

- `on-add_zz-gamify.sh`, `on-modify_zz-gamify.sh`: score the change. The `zz` makes them run last, so a hook that rejects a command does so before any points are paid.
- `on-launch_gamify.sh`: shows the last verdict, and at most every 30 minutes starts `gamify --refresh`, detached. It starts no Python otherwise.
- `gamify --refresh`: judges finished days and saves a snapshot of urgencies (`logs/gamify-snapshot.json`). Hook input has no urgency in it, so hooks read this; a task's urgency is the one it had before you started adjusting it.
- `config/gamify-ledger.jsonl`: the ledger, append-only. The score is always added up from it.

**Between stations.** gittw merges the ledger with `merge=union` (the installer adds the line to `~/.task/.gitattributes`). Judgements have the same id on every station, so a day judged twice counts once.

## Limits

- A finished day is judged against your tasks as they stand now, so finishing a task before the sweep runs spares it that day's rot.
- A task added since the last snapshot (up to 30 minutes) has an urgency factor of 1.
- `tw-web` shows remarks in its warning style.

## `task undo` of an add

Taskwarrior 2.6.2 gives on-modify hooks `{}` as the new task when you undo an add, then refuses `{}` back (`Hook Error: Expected 1 JSON task(s), found 0`). So a hook that passes its input through makes that undo fail, every time, and everything older in the undo stack is out of reach. gamify hands back the old task, which lets the undo through, but it runs last: an earlier on-modify hook that passes `{}` along still stops it.

## Tests

```bash
pytest tests     # a real Taskwarrior in a temp folder; never touches ~/.task
```

## License

MIT
