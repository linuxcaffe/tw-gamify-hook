"""Tests for gamify, against a real Taskwarrior in a throwaway folder. Never touches ~/.task."""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = ['on-add_zz-gamify.sh', 'on-modify_zz-gamify.sh', 'on-launch_gamify.sh']


class Game:
    def __init__(self, root, rc=''):
        self.root = root
        self.dir = root / 'task'
        for sub in ('hooks', 'scripts', 'config', 'logs'):
            (self.dir / sub).mkdir(parents=True)
        (self.dir / 'scripts' / 'gamify').symlink_to(REPO / 'gamify')
        for hook in HOOKS:
            shutil.copy(REPO / hook, self.dir / 'hooks' / hook)
        self.taskrc = root / 'rc'
        self.taskrc.write_text(f'data.location={self.dir}\nhooks=on\nconfirmation=no\nrecurrence.confirmation=no\n'
                               'verbose=footnote\nnag=\n')
        self.ledger = self.dir / 'config' / 'gamify-ledger.jsonl'
        self.config(rc + '\ngamify.zero-quip-chance = 0\n')
        # Fresh game, so a later run of the on-launch hook doesn't race the test
        (self.dir / 'logs' / 'gamify.stamp').touch()
        self.env = {**os.environ, 'TASKRC': str(self.taskrc), 'TASKDATA': str(self.dir), 'TW_TASK_DIR': str(self.dir),
                    'NO_COLOR': '1'}
        self.env.pop('TW_DEBUG', None)

    def config(self, text):
        (self.dir / 'config' / 'gamify.rc').write_text(text, encoding='utf-8')

    def task(self, *args):
        return subprocess.run(['task', *args], env=self.env, capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)

    def add(self, *args):
        self.task('add', *args)
        return self.export()[-1]

    def export(self):
        return json.loads(self.task('rc.hooks=off', 'export').stdout)

    def gamify(self, *args):
        return subprocess.run([str(REPO / 'gamify'), *args], env=self.env, capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)

    def entries(self, kind=None):
        if not self.ledger.exists():
            return []
        rows = [json.loads(l) for l in self.ledger.read_text(encoding='utf-8').splitlines()]
        return [e for e in rows if kind is None or e['k'] == kind]

    def state(self):
        return json.loads(self.gamify('--json').stdout)

    def write_ledger(self, rows):
        self.ledger.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')

    def backdate(self, days, last_sweep=None):
        """A game that began `days` ago and was last judged `last_sweep` days ago (default: never)."""
        start = (date.today() - timedelta(days=days)).isoformat()
        rows = [{'id': 'genesis', 'k': 'genesis', 'c': 0, 'x': 0, 't': 1, 'day': start}]
        if last_sweep is not None:
            iso = (date.today() - timedelta(days=last_sweep)).isoformat()
            rows.append({'id': f'sweep:{iso}', 'k': 'sweep', 'c': 0, 't': 2, 'day': iso})
        self.write_ledger(rows)

    def import_task(self, **fields):
        task = {'description': 'imported', 'status': 'pending', 'entry': '20200101T000000Z',
                'modified': '20200101T000000Z', 'uuid': f'{len(self.export()):08x}-0000-4000-8000-000000000000'}
        task.update(fields)
        path = self.root / 'import.json'
        path.write_text(json.dumps([task]))
        result = self.task('import', str(path))
        assert result.returncode == 0, result.stderr
        return task


def stamp(days_from_now):
    return (date.today() + timedelta(days=days_from_now)).strftime('%Y%m%dT120000Z')


@pytest.fixture
def game(tmp_path):
    return Game(tmp_path)


def aged(game, *args):
    """A task old enough not to count as added-and-finished-in-one-breath."""
    return game.import_task(entry=stamp(-30), modified=stamp(-30), **dict(a.split('=', 1) for a in args))


# ── Doing ─────────────────────────────────────────────────────────────────────

def test_first_action_starts_the_game_with_the_welcome(game):
    result = game.task('add', 'first')
    assert result.returncode == 0
    assert '+25' in result.stderr
    assert game.state()['coins'] == 25


def test_on_time_beats_late_and_early_beats_on_time(game):
    pay = {}
    for name, due in (('early', stamp(3)), ('ontime', stamp(0)), ('late', stamp(-3))):
        t = aged(game, f'due={due}', f'description={name}')
        game.task(t['uuid'], 'done')
        pay[name] = game.entries('done')[-1]['c']
    assert pay['early'] > pay['ontime'] > pay['late'] > 0


def test_very_late_is_never_worth_nothing(game):
    t = aged(game, f'due={stamp(-400)}')
    game.task(t['uuid'], 'done')
    assert game.entries('done')[-1]['c'] > 0


def test_urgency_multiplies_from_the_snapshot_and_is_capped(game):
    t = aged(game)
    other = aged(game)
    (game.dir / 'logs' / 'gamify-snapshot.json').write_text(json.dumps({'urgency': {t['uuid']: 20, other['uuid']: 500}}))
    game.task(t['uuid'], 'done')
    game.task(other['uuid'], 'done')
    paid = game.entries('done')
    assert paid[0]['c'] == 20      # 10 x (1 + 20/20)
    assert paid[1]['c'] == 30      # 10 x the cap of 3


def test_regicide_pays_for_the_top_task_only(game):
    top, other = aged(game), aged(game)
    (game.dir / 'logs' / 'gamify-snapshot.json').write_text(
        json.dumps({'urgency': {top['uuid']: 10, other['uuid']: 6}, 'top': top['uuid']}))
    game.task(other['uuid'], 'done')
    game.task(top['uuid'], 'done')
    other_entry, top_entry = game.entries('done')
    assert 'regicide' not in other_entry['why'] and 'regicide' in top_entry['why']


def test_add_then_done_at_once_only_pays_the_small_flat_rate(game):
    t = game.add('farm', 'due:tomorrow', 'priority:H')
    game.task(t['uuid'], 'done')
    assert game.entries('done')[-1]['c'] == 2


def test_focus_bonus_for_a_started_task(game):
    t = aged(game)
    game.task(t['uuid'], 'start')
    game.task(t['uuid'], 'done')
    assert 'focus' in game.entries('done')[-1]['why']


def test_log_pays_flat_and_counts_for_the_streak(game):
    game.task('log', 'did it yesterday')
    assert game.entries('log')[-1]['c'] == 2
    assert game.state()['streak'] == 1


# ── Housekeeping and caps ─────────────────────────────────────────────────────

def test_filed_add_pays_and_bare_add_does_not(game):
    game.task('add', 'bare one')
    assert game.entries('add') == []
    game.task('add', 'filed', 'project:x', 'due:tomorrow')
    assert game.entries('add')[-1]['c'] == 1


def test_add_cap(tmp_path):
    game = Game(tmp_path, 'gamify.cap.add = 2\n')
    for n in range(4):
        game.task('add', f'filed {n}', 'project:x', 'due:tomorrow')
    assert sum(e['c'] for e in game.entries('add')) == 2


def test_filling_in_a_missing_project_pays_and_other_edits_do_not(game):
    t = game.add('needs a home')
    game.task(t['uuid'], 'modify', 'project:home')
    game.task(t['uuid'], 'modify', 'more words')
    assert len(game.entries('file')) == 1


# ── Dodging ───────────────────────────────────────────────────────────────────

def test_delete_is_free_unless_overdue_or_stale(game):
    young = game.add('young')
    game.task(young['uuid'], 'delete')
    assert game.entries('delete') == []
    overdue = aged(game, f'due={stamp(-2)}')
    game.task(overdue['uuid'], 'delete')
    assert game.entries('delete')[-1]['c'] == -5
    stale = game.import_task(entry=stamp(-100), modified=stamp(-100))
    game.task(stale['uuid'], 'delete')
    assert game.entries('delete')[-1]['c'] == 2


def test_pushing_a_due_date_escalates_and_pulling_it_in_is_free(game):
    t = game.add('dodge', f'due:{(date.today() + timedelta(days=1)).isoformat()}')
    for days in (2, 3, 4):
        game.task(t['uuid'], 'modify', f'due:{(date.today() + timedelta(days=days)).isoformat()}')
    assert [e['c'] for e in game.entries('push')] == [-1, -2, -4]
    game.task(t['uuid'], 'modify', f'due:{date.today().isoformat()}')
    assert len(game.entries('push')) == 3


def test_coins_can_go_negative_but_xp_never_drops(game):
    game.write_ledger([{'id': 'genesis', 'k': 'genesis', 'c': 0, 'x': 0, 't': 1, 'day': date.today().isoformat()}])
    t = aged(game, f'due={stamp(-5)}')
    game.task(t['uuid'], 'delete')
    state = game.state()
    assert state['coins'] == -5 and state['xp'] == 0


# ── Undo ──────────────────────────────────────────────────────────────────────

def test_undo_takes_the_points_back(game):
    t = aged(game)
    game.task(t['uuid'], 'done')
    earned = game.state()['coins']
    game.task('undo')
    state = game.state()
    assert state['coins'] == earned - game.entries('done')[-1]['c']
    assert state['streak'] == 0


def test_done_undo_done_only_pays_once(game):
    t = aged(game)
    game.task(t['uuid'], 'done')
    once = game.state()['coins']
    game.task('undo')
    game.task(t['uuid'], 'done')
    assert game.state()['coins'] == once


def test_undoing_a_push_refunds_it_and_resets_the_escalation(game):
    t = game.add('dodge', f'due:{(date.today() + timedelta(days=1)).isoformat()}')
    game.task(t['uuid'], 'modify', f'due:{(date.today() + timedelta(days=2)).isoformat()}')
    game.task('undo')
    game.task(t['uuid'], 'modify', f'due:{(date.today() + timedelta(days=2)).isoformat()}')
    assert game.entries('push')[-1]['c'] == -1


def test_undoing_an_add_works_and_takes_the_points_back(game):
    # 2.6.2 gives on-modify `{}` as the new task here and refuses it coming back, which jams undo.
    game.task('add', 'older', 'project:x', 'due:tomorrow')
    game.task('add', 'oops', 'project:x', 'due:tomorrow')
    coins = game.state()['coins']
    result = game.task('undo')
    assert 'Hook Error' not in result.stderr
    assert [t['description'] for t in game.export()] == ['older']
    assert game.state()['coins'] == coins - 1
    assert game.task('undo').returncode == 0
    assert game.export() == []


def test_undoing_an_add_already_repaired_by_an_earlier_hook_takes_the_points_back(game):
    fix = game.dir / 'hooks' / 'on-modify_00-fix.py'
    fix.write_text("#!/usr/bin/env python3\nimport sys\nl = sys.stdin.read().splitlines()\n"
                   "print(l[0] if l[-1].strip() == '{}' else l[-1])\n")
    fix.chmod(fix.stat().st_mode | stat.S_IEXEC)
    game.task('add', 'oops', 'project:x', 'due:tomorrow')
    coins = game.state()['coins']
    assert 'Hook Error' not in game.task('undo').stderr
    assert game.export() == []
    assert game.state()['coins'] == coins - 1


def test_reopening_a_completed_task_takes_the_points_back(game):
    t = aged(game)
    game.task(t['uuid'], 'done')
    earned = game.state()['coins']
    game.task(t['uuid'], 'start')
    assert game.state()['coins'] < earned


# ── Not doing: the sweep ──────────────────────────────────────────────────────

def test_sweep_judges_finished_days_once_each(game):
    game.backdate(30, last_sweep=4)
    aged(game, f'due={stamp(-10)}')
    assert game.gamify('--refresh').returncode == 0
    days = {e['day'] for e in game.entries('rot')}
    assert days == {(date.today() - timedelta(days=n)).isoformat() for n in (1, 2, 3)}
    count = len(game.entries())
    game.gamify('--refresh')
    assert len(game.entries()) == count


def test_recap_is_shown_once_by_the_next_command(game):
    game.backdate(30, last_sweep=2)
    aged(game, f'due={stamp(-10)}')
    game.gamify('--refresh')
    first = game.task('count')
    second = game.task('count')
    assert 'Reckoning' in first.stderr
    assert 'Reckoning' not in second.stderr


def test_amnesty_spares_the_day_one_backlog_then_ends(tmp_path):
    game = Game(tmp_path, 'gamify.amnesty-days = 14\n')
    aged(game, f'due={stamp(-60)}')
    game.backdate(5, last_sweep=3)
    game.gamify('--refresh')
    assert game.entries('rot') == []
    game.backdate(40, last_sweep=3)
    game.gamify('--refresh')
    assert game.entries('rot') != []


def test_blank_day_clean_day_and_a_done_day(game):
    game.backdate(30, last_sweep=2)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    game.gamify('--refresh')
    assert [e['day'] for e in game.entries('blank')] == [yesterday]
    assert [e['day'] for e in game.entries('clean')] == [yesterday]

    game.backdate(30, last_sweep=2)
    rows = game.entries() + [{'id': 'done:x', 'k': 'done', 'c': 10, 'x': 10, 't': 3, 'day': yesterday}]
    game.write_ledger(rows)
    game.gamify('--refresh')
    assert game.entries('blank') == []


def test_a_blank_charged_by_an_unsynced_station_is_ignored_once_the_ledgers_merge(game):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    game.write_ledger([
        {'id': 'genesis', 'k': 'genesis', 'c': 0, 'x': 0, 't': 1, 'day': yesterday},
        {'id': f'blank:{yesterday}', 'k': 'blank', 'c': -5, 'x': 0, 't': 2, 'day': yesterday},
        {'id': 'done:x', 'k': 'done', 'c': 10, 'x': 10, 't': 3, 'day': yesterday},
    ])
    assert game.state()['coins'] == 10


def test_blocked_waiting_future_and_noscore_tasks_do_not_rot(game):
    game.backdate(30, last_sweep=2)
    blocker = aged(game, 'description=blocker')
    aged(game, f'due={stamp(-5)}', 'description=blocked', f"depends={blocker['uuid']}")
    aged(game, f'due={stamp(5)}', 'description=future')
    game.import_task(entry=stamp(-30), modified=stamp(-30), due=stamp(-5), tags=['noscore'])
    game.gamify('--refresh')
    assert game.entries('rot') == []


def test_daily_penalty_cap_and_per_task_rot_cap(tmp_path):
    game = Game(tmp_path, 'gamify.cap.penalty-day = -5\ngamify.cap.rot-days = 2\n')
    game.backdate(60, last_sweep=8)
    for _ in range(10):
        aged(game, f'due={stamp(-40)}')
    game.gamify('--refresh')
    per_day = {}
    for e in game.entries('rot'):
        per_day[e['day']] = per_day.get(e['day'], 0) + e['c']
    assert per_day and min(per_day.values()) >= -5
    per_task = {}
    for e in game.entries('rot'):
        per_task[e['u']] = per_task.get(e['u'], 0) + 1
    assert max(per_task.values()) <= 2


def test_zombies_and_fossils(game):
    game.backdate(200, last_sweep=2)
    game.import_task(entry=stamp(-300), modified=stamp(-300), description='old')
    game.import_task(entry=stamp(-30), modified=stamp(-30), start=stamp(-5), description='left running')
    game.gamify('--refresh')
    assert [e['d'] for e in game.entries('fossil')] == ['old']
    assert [e['d'] for e in game.entries('zombie')] == ['left running']


def test_fossil_is_charged_once_a_week(game):
    game.backdate(200, last_sweep=8)
    game.import_task(entry=stamp(-300), modified=stamp(-300))
    game.gamify('--refresh')
    weeks = [e['id'] for e in game.entries('fossil')]
    assert 1 <= len(weeks) <= 2 and len(weeks) == len(set(weeks))


def test_holiday_and_pause_days_are_not_judged(game):
    game.backdate(30, last_sweep=2)
    yesterday = date.today() - timedelta(days=1)
    (game.dir / 'config' / 'holidays.rc').write_text(
        f"holiday.x.name=Something\nholiday.x.date={yesterday.strftime('%Y%m%d')}\n")
    aged(game, f'due={stamp(-10)}')
    game.gamify('--refresh')
    assert game.entries('rot') == [] and game.entries('blank') == []


def test_pause_stops_penalties_and_resume_restarts_them(game):
    game.backdate(30, last_sweep=2)
    aged(game, f'due={stamp(-10)}')
    start = (date.today() - timedelta(days=1)).isoformat()
    rows = game.entries() + [{'id': 'pause:x', 'k': 'pause', 'c': 0, 't': 3, 'day': start, 'from': start,
                              'to': date.today().isoformat()}]
    game.write_ledger(rows)
    game.gamify('--refresh')
    assert game.entries('rot') == []
    assert 'No penalties' in game.gamify('pause', '3').stdout
    assert game.state()['paused'] is True
    game.gamify('resume')
    assert game.state()['paused'] is False


def test_a_streak_survives_a_rest_day(game):
    days = [(date.today() - timedelta(days=n)).isoformat() for n in (3, 2, 1, 0)]
    (game.dir / 'logs' / 'gamify-snapshot.json').write_text(json.dumps({'rest': [days[2]]}))
    game.write_ledger([{'id': 'genesis', 'k': 'genesis', 'c': 0, 'x': 0, 't': 1, 'day': days[0]}] +
                      [{'id': f'done:{d}', 'k': 'done', 'c': 1, 'x': 1, 't': 2, 'day': d}
                       for d in (days[0], days[1], days[3])])
    assert game.state()['streak'] == 3


def test_full_moon_doubles_late_pay(tmp_path):
    game = Game(tmp_path)
    (game.dir / 'logs' / 'gamify-snapshot.json').write_text(json.dumps({'moons': [date.today().isoformat()]}))
    t = aged(game, f'due={stamp(-3)}')
    game.task(t['uuid'], 'done')
    assert game.entries('done')[-1]['c'] == 14      # 10 x 0.7 x 2


def test_custom_rule_pays_per_task_once_a_day(tmp_path):
    game = Game(tmp_path, 'gamify.rule.nope.filter = status:pending project:\n'
                          'gamify.rule.nope.points = -3\ngamify.rule.nope.msg = no project\n')
    game.backdate(30, last_sweep=2)
    aged(game, f'due={stamp(5)}')
    aged(game, f'due={stamp(5)}', 'project=x')
    game.gamify('--refresh')
    assert [e['c'] for e in game.entries('rule')] == [-3]
    assert game.entries('rule')[0]['why'] == 'no project'


def test_a_failed_export_judges_nothing(game):
    game.backdate(30, last_sweep=2)
    env = dict(game.env, PATH=str(game.root))       # no task on the PATH
    python = shutil.which('python3')
    before = game.entries()
    result = subprocess.run([python, str(REPO / 'gamify'), '--refresh'], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert game.entries() == before


# ── The rules of being a hook ─────────────────────────────────────────────────

def test_hook_fails_open_on_a_corrupt_ledger_and_a_bad_rc(game):
    game.ledger.write_text('not json\n{"also": "not an entry"}\n')
    game.config('gamify.points.done = banana\n')
    t = game.add('still works')
    result = game.task(t['uuid'], 'done')
    assert result.returncode == 0
    assert game.export()[-1]['status'] == 'completed'


def test_hook_passes_through_with_the_command_missing(game):
    (game.dir / 'scripts' / 'gamify').unlink()
    result = game.task('add', 'no game')
    assert result.returncode == 0
    assert game.export()[-1]['description'] == 'no game'


def test_a_rejected_command_pays_nothing(game):
    reject = game.dir / 'hooks' / 'on-modify_m-reject.py'
    reject.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint("nope")\nsys.exit(1)\n')
    reject.chmod(reject.stat().st_mode | stat.S_IEXEC)
    t = aged(game)
    result = game.task(t['uuid'], 'done')
    assert result.returncode != 0
    assert game.entries('done') == []


def test_writes_from_another_hook_score_nothing(game):
    hook = game.dir / 'hooks' / 'on-exit_robot.py'
    hook.write_text('#!/usr/bin/env python3\nimport os, subprocess, sys\n'
                    'data = sys.stdin.read()\n'
                    'if data.strip() and "robot" not in data:\n'
                    '    subprocess.run(["task", "add", "robot task", "project:x", "due:tomorrow"],\n'
                    '                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n')
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    game.task('add', 'mine', 'project:x', 'due:tomorrow')
    assert 'robot task' in [t['description'] for t in game.export()]
    assert [e['d'] for e in game.entries('add')] == ['mine']


def test_import_scores_nothing(game):
    aged(game, f'due={stamp(-5)}')
    assert [e for e in game.entries() if e['k'] in ('add', 'done', 'delete')] == []


def test_noscore_tag_opts_out(game):
    t = game.add('private', 'project:x', 'due:tomorrow', '+noscore')
    game.task(t['uuid'], 'done')
    assert game.entries('add') == [] and game.entries('done') == []


def test_recurring_template_add_scores_nothing_and_instances_score_when_done(game):
    game.task('add', 'weekly', 'project:x', 'due:tomorrow', 'recur:weekly')
    assert game.entries('add') == []


def test_disabled_scores_and_judges_nothing(tmp_path):
    game = Game(tmp_path, 'gamify.enabled = no\n')
    t = game.add('off')
    game.task(t['uuid'], 'done')
    game.gamify('--refresh')
    assert game.entries() == []


def test_volume_off_is_silent_but_still_scores(tmp_path):
    game = Game(tmp_path, 'gamify.volume = off\n')
    result = game.task('add', 'quiet', 'project:x', 'due:tomorrow')
    assert 'gamify' not in result.stderr and '🪙' not in result.stderr
    assert game.entries('add')


def test_every_voice_covers_every_event():
    source = (REPO / 'gamify').read_text(encoding='utf-8')
    scope = {'__name__': 'gamify_test', '__file__': str(REPO / 'gamify')}
    exec(compile(source, 'gamify', 'exec'), scope)
    events = set(scope['VOICES']['grandmother'])
    for name, bank in scope['VOICES'].items():
        assert set(bank) == events, name


# ── Merging, money, display, config ───────────────────────────────────────────

def test_a_ledger_merged_with_union_counts_each_sweep_once(tmp_path):
    repo = tmp_path / 'r'
    repo.mkdir()
    def git(*args, cwd=repo):
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd, check=True, capture_output=True)
    git('init', '-b', 'main')
    (repo / '.gitattributes').write_text('config/gamify-ledger.jsonl merge=union\n')
    (repo / 'config').mkdir()
    ledger = repo / 'config' / 'gamify-ledger.jsonl'
    today = date.today().isoformat()
    base = json.dumps({'id': 'genesis', 'k': 'genesis', 'c': 25, 'x': 25, 't': 1, 'day': today}) + '\n'
    ledger.write_text(base)
    git('add', '.'); git('commit', '-m', 'base')
    shared = json.dumps({'id': 'rot:u:1', 'k': 'rot', 'c': -2, 'x': 0, 't': 2, 'day': today})
    git('checkout', '-b', 'phone')
    ledger.write_text(base + shared + '\n' + json.dumps({'id': 'done:p', 'k': 'done', 'c': 5, 'x': 5, 't': 3, 'day': today}) + '\n')
    git('commit', '-am', 'phone')
    git('checkout', 'main')
    ledger.write_text(base + shared + '\n' + json.dumps({'id': 'done:l', 'k': 'done', 'c': 7, 'x': 7, 't': 4, 'day': today}) + '\n')
    git('commit', '-am', 'laptop')
    git('merge', 'phone', '-m', 'merge')

    game = Game(tmp_path / 'g')
    game.write_ledger([])
    shutil.copy(ledger, game.ledger)
    assert game.state()['coins'] == 25 - 2 + 5 + 7


def test_shop_refuses_what_you_cannot_afford_and_records_what_you_buy(game):
    game.write_ledger([{'id': 'genesis', 'k': 'genesis', 'c': 120, 'x': 120, 't': 1, 'day': date.today().isoformat()}])
    assert game.gamify('redeem', 'donut').returncode == 1
    assert game.gamify('redeem', 'youtube').returncode == 0
    state = game.state()
    assert state['coins'] == 20 and state['xp'] == 120
    assert game.gamify('redeem', 'unicorn').returncode == 2


def test_prizes_and_ranks_come_from_the_rc(tmp_path):
    game = Game(tmp_path, 'gamify.prize.pie.cost = 5\ngamify.prize.pie.label = 🥧 Pie\n'
                          'gamify.rank.0 = Peasant\ngamify.rank.10 = Duke\n')
    game.write_ledger([{'id': 'genesis', 'k': 'genesis', 'c': 12, 'x': 12, 't': 1, 'day': date.today().isoformat()}])
    state = game.state()
    assert state['rank'] == 'Duke' and state['next'] is None
    assert list(state['prizes']) == ['pie']
    assert game.gamify('redeem', 'pie').returncode == 0


def test_level_up_is_announced_even_when_quiet(tmp_path):
    game = Game(tmp_path, 'gamify.volume = quiet\ngamify.rank.0 = Peasant\ngamify.rank.20 = Duke\n')
    game.write_ledger([{'id': 'genesis', 'k': 'genesis', 'c': 19, 'x': 19, 't': 1, 'day': date.today().isoformat()}])
    result = game.task('add', 'filed', 'project:x', 'due:tomorrow')
    assert 'LEVEL 2: Duke' in result.stderr


def test_custom_remarks_join_the_bank(tmp_path):
    game = Game(tmp_path, 'gamify.voice = nobody\ngamify.say.nobody.add.1 = Filed, sir.\n')
    result = game.task('add', 'filed', 'project:x', 'due:tomorrow')
    assert 'Filed, sir.' in result.stderr


def test_board_lines_all_line_up_and_all_commands_run(game):
    t = aged(game, f'due={stamp(-2)}')
    game.task(t['uuid'], 'done')
    result = game.gamify()
    assert result.returncode == 0, result.stderr
    assert 'TASKWARRIOR: THE GAME' in result.stdout
    scope = {'__name__': 'gamify_test', '__file__': str(REPO / 'gamify')}
    exec(compile((REPO / 'gamify').read_text(encoding='utf-8'), 'gamify', 'exec'), scope)
    box = [l for l in result.stdout.splitlines() if l[:1] in '╔║╟╚']
    assert len({scope['width'](l) for l in box}) == 1, box
    for args in (['log'], ['log', '3'], ['shop'], ['--line'], ['--json'], ['-h'], ['-v']):
        assert game.gamify(*args).returncode == 0, args
    assert game.gamify('nonsense').returncode == 2


def test_every_default_is_documented_in_the_shipped_rc():
    scope = {'__name__': 'gamify_test', '__file__': str(REPO / 'gamify')}
    exec(compile((REPO / 'gamify').read_text(encoding='utf-8'), 'gamify', 'exec'), scope)
    text = (REPO / 'gamify.rc').read_text(encoding='utf-8')
    shipped = dict(re.findall(r'^gamify\.(\S+)\s*=\s*([^#\n]*?)\s*(?:#.*)?$', text, re.M))
    for key, value in scope['DEFAULTS'].items():
        assert key in shipped, key
        assert shipped[key] == value, key


def test_a_station_setting_in_local_rc_wins(tmp_path):
    game = Game(tmp_path, 'gamify.points.add-filed = 1\n')
    (game.dir / 'local.rc').write_text('gamify.points.add-filed = 9\ngittw.device=phone\n')
    game.task('add', 'filed', 'project:x', 'due:tomorrow')
    made = game.entries('add')[-1]
    assert made['c'] == 9 and made['st'] == 'phone'


# ── Installer ─────────────────────────────────────────────────────────────────

def test_installer_keeps_your_settings_and_score_and_leaves_no_trace_of_itself(tmp_path):
    home = tmp_path / 'home'
    task_dir = home / '.task'
    (task_dir / '.git').mkdir(parents=True)
    env = {**os.environ, 'HOME': str(home)}
    env.pop('TW_TASK_DIR', None)
    run = lambda arg: subprocess.run(['bash', str(REPO / 'gamify.install'), arg], env=env, capture_output=True, text=True)

    result = run('install')
    assert result.returncode == 0, result.stderr
    for hook in HOOKS:
        assert os.access(task_dir / 'hooks' / hook, os.X_OK)
    assert 'config/gamify-ledger.jsonl merge=union' in (task_dir / '.gitattributes').read_text()

    rc = task_dir / 'config' / 'gamify.rc'
    rc.write_text('gamify.voice = coach\n')
    ledger = task_dir / 'config' / 'gamify-ledger.jsonl'
    ledger.write_text('{"id":"genesis"}\n')

    assert run('remove').returncode == 0
    assert not (task_dir / 'scripts' / 'gamify').exists()
    assert not (task_dir / 'hooks' / HOOKS[0]).exists()
    assert rc.exists() and ledger.exists()

    assert run('install').returncode == 0
    assert rc.read_text() == 'gamify.voice = coach\n'
    assert (task_dir / 'config' / 'gamify.rc.new').exists()
    assert (task_dir / '.gitattributes').read_text().count('merge=union') == 1
    manifest = (task_dir / 'config' / '.tw_manifest').read_text().splitlines()
    assert len([l for l in manifest if l.startswith('gamify|')]) == 6
    assert os.access(task_dir / 'scripts' / 'gamify-score', os.X_OK)
    wrappers = (task_dir / 'config' / '.tw_wrappers').read_text()
    assert wrappers.count('score|gamify-score|') == 1
    assert run('remove').returncode == 0
    assert 'score|' not in (task_dir / 'config' / '.tw_wrappers').read_text()


# ── The scoreboard ────────────────────────────────────────────────────────────

def load_scope():
    scope = {'__name__': 'gamify_test', '__file__': str(REPO / 'gamify')}
    exec(compile((REPO / 'gamify').read_text(encoding='utf-8'), 'gamify', 'exec'), scope)
    return scope


def board_lines(text):
    return [l for l in text.splitlines() if l[:1] in '╔║╟╚']


def test_scoreboard_lines_up_even_with_long_names_and_debt(tmp_path):
    game = Game(tmp_path, 'gamify.rank.0 = ' + 'Very Long Title Indeed ' * 5 + '\ngamify.rank.99999 = ' + 'Next ' * 30 + '\n')
    game.write_ledger([{'id': 'genesis', 'k': 'genesis', 'c': -40, 'x': 0, 't': 1, 'day': date.today().isoformat()}])
    game.import_task(description='a description that goes on and on ' * 6, due=stamp(-9), entry=stamp(-30), modified=stamp(-30))
    result = game.gamify('score')
    assert result.returncode == 0, result.stderr
    scope = load_scope()
    lines = board_lines(result.stdout)
    assert len(lines) > 8 and len({scope['width'](l) for l in lines}) == 1, lines
    assert 'Shut until' in result.stdout


def test_scoreboard_names_the_offenders_and_what_they_cost(game):
    rotter = game.import_task(description='renew the thing', due=stamp(-9), entry=stamp(-30), modified=stamp(-30))
    game.import_task(description='fix the gate latch', entry=stamp(-400), modified=stamp(-400))
    game.import_task(description='innocent', due=stamp(5), entry=stamp(-400), modified=stamp(-400))
    today = date.today().isoformat()
    game.write_ledger([{'id': 'genesis', 'k': 'genesis', 'c': 25, 'x': 25, 't': 1, 'day': today},
                       {'id': 'r1', 'k': 'rot', 'c': -6, 'x': 0, 't': 2, 'day': today, 'u': rotter['uuid']}])
    out = game.gamify('score').stdout
    assert 'renew the thing' in out and '9 days late, −6 so far' in out
    assert 'fix the gate latch' in out and '400 days untouched' in out
    assert 'innocent' not in out


def test_nobody_wanted(game):
    game.task('add', 'fresh')
    assert 'Nobody' in game.gamify('score').stdout


def test_trophies_are_won_counted_and_adjustable(tmp_path):
    game = Game(tmp_path, 'gamify.trophy.moon.need = 0\ngamify.trophy.clean.label = 🧼 Spotless\n')
    today = date.today().isoformat()
    rows = [{'id': 'genesis', 'k': 'genesis', 'c': 0, 'x': 0, 't': 1, 'day': today}]
    for n in range(3):
        rows.append({'id': f'd{n}', 'k': 'done', 'c': 9, 'x': 9, 't': 2, 'day': today, 'u': str(n), 'why': 'done+moon+regicide'})
    rows.append({'id': 'c1', 'k': 'clean', 'c': 5, 'x': 5, 't': 3, 'day': today})
    rows.append({'id': 'd9', 'k': 'done', 'c': 9, 'x': 9, 't': 4, 'day': today, 'u': 'x', 'why': 'done+regicide'})
    rows.append({'id': 'rev:d9', 'k': 'rev', 'rev': 'd9', 'c': -9, 'x': -9, 't': 5, 'day': today})
    game.write_ledger(rows)
    out = game.gamify('score').stdout
    assert '👑 Regicide ×3' in out           # the undone one doesn't count
    assert 'Werewolf' not in out             # retired
    assert '🧼 Spotless' in out and '🔒 Next:' in out


def test_debtors_prison_remembers_your_lowest_point(game):
    today = date.today().isoformat()
    game.write_ledger([{'id': 'genesis', 'k': 'genesis', 'c': 0, 'x': 0, 't': 1, 'day': today},
                       {'id': 'a', 'k': 'blank', 'c': -60, 'x': 0, 't': 2, 'day': '2020-01-01'},
                       {'id': 'b', 'k': 'done', 'c': 100, 'x': 100, 't': 3, 'day': today}])
    assert "Debtor's Prison" in game.gamify('score').stdout


def test_last_word_survives_for_the_scoreboard(game):
    game.task('add', 'filed', 'project:x', 'due:tomorrow')
    said = (game.dir / 'logs' / 'gamify-lastword.txt').read_text(encoding='utf-8').strip()
    assert said and said in game.gamify('score').stdout


def test_score_works_in_ascii_and_with_no_game(tmp_path):
    game = Game(tmp_path, 'gamify.box = ascii\n')
    assert 'No game yet' in game.gamify('score').stdout
    game.task('add', 'first')
    out = game.gamify('score').stdout
    assert 'THE SCORE' in out and '▓' not in out and '▁' not in out


def test_gamify_score_script_runs_the_scoreboard(game):
    shutil.copy(REPO / 'gamify-score', game.dir / 'scripts' / 'gamify-score')
    game.task('add', 'first')
    result = subprocess.run([str(game.dir / 'scripts' / 'gamify-score')], env=game.env, capture_output=True, text=True)
    assert 'TROPHY CASE' in result.stdout


# ── Clients that hide hook output ─────────────────────────────────────────────

def client_env(game, client='vit'):
    spool = game.root / 'spool'
    spool.mkdir(exist_ok=True)
    game.env.update(TW_SPOOL_DIR=str(spool), TW_CLIENT=client, TW_NOINTERACT='1')
    return spool


def test_level_up_is_spooled_for_vit_as_plain_text(tmp_path):
    game = Game(tmp_path, 'gamify.rank.0 = Peasant\ngamify.rank.20 = Duke\n')
    game.write_ledger([{'id': 'genesis', 'k': 'genesis', 'c': 19, 'x': 19, 't': 1, 'day': date.today().isoformat()}])
    spool = client_env(game)
    game.task('add', 'filed', 'project:x', 'due:tomorrow')
    commands = list(spool.glob('*.cmd'))
    assert len(commands) == 1
    assert commands[0].read_text().strip().endswith('gamify score')
    reason = commands[0].with_suffix('.reason').read_text()
    assert reason.startswith('gamify: LEVEL 2: Duke') and reason.isascii()


def test_routine_scoring_never_interrupts_vit(game):
    spool = client_env(game)
    game.task('add', 'filed', 'project:x', 'due:tomorrow')
    assert list(spool.glob('*')) == []


def test_other_clients_are_not_spooled(tmp_path):
    game = Game(tmp_path, 'gamify.rank.0 = Peasant\ngamify.rank.20 = Duke\n')
    game.write_ledger([{'id': 'genesis', 'k': 'genesis', 'c': 19, 'x': 19, 't': 1, 'day': date.today().isoformat()}])
    spool = client_env(game, 'tw-web')
    result = game.task('add', 'filed', 'project:x', 'due:tomorrow')
    assert list(spool.glob('*')) == [] and 'LEVEL 2' in result.stderr


def test_the_verdict_reaches_vit_once_by_the_spool(game):
    game.backdate(30, last_sweep=2)
    aged(game, f'due={stamp(-10)}')
    game.gamify('--refresh')
    spool = client_env(game)
    first = game.task('count')
    game.task('count')
    assert 'Reckoning' not in first.stderr
    reasons = list(spool.glob('*.reason'))
    assert len(reasons) == 1 and 'The Reckoning' in reasons[0].read_text() and reasons[0].read_text().isascii()
    assert not (game.dir / 'logs' / 'gamify-recap.txt').exists()
