import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hub


def result(ok=True, limited=False, answer='done'):
    return {'ok': ok, 'limited': limited, 'answer': answer, 'usage': None, 'raw': ''}


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patches = [mock.patch.object(hub, 'STATE', Path(self.tmp.name)),
                   mock.patch.object(hub, 'git_status', lambda cwd: None),
                   mock.patch.dict(os.environ, {}, clear=False)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop('HUB_DEPTH', None)
        self.addCleanup(self.tmp.cleanup)

    def go(self, quota, executors, kind='implement'):
        with mock.patch.object(hub, 'status', lambda: quota), mock.patch.dict(hub.EXECUTORS, executors):
            return hub.run('task', kind=kind, cwd=self.tmp.name)

    def test_codex_out_of_quota_goes_to_claude(self):
        r = self.go({'codex': {'ok': False}, 'claude': {'ok': True}, 'local': {'ok': True}},
                    {'claude': lambda *a: result()})
        self.assertEqual(r['who'], 'claude')
        self.assertEqual(r['tried'][0], {'who': 'codex', 'skipped': 'quota'})
        self.assertFalse(r['needs_review'])

    def test_limit_error_blocks_and_falls_through(self):
        r = self.go({'codex': {'ok': None}, 'claude': {'ok': True}, 'local': {'ok': True}},
                    {'codex': lambda *a: result(False, True), 'claude': lambda *a: result()})
        self.assertEqual(r['who'], 'claude')
        self.assertIn('codex', hub.read_json(hub.STATE / 'blocked.json', {}))

    def test_both_clouds_out_local_marks_review(self):
        r = self.go({'codex': {'ok': False}, 'claude': {'ok': False}, 'local': {'ok': True}},
                    {'local': lambda *a: result()}, kind='design')
        self.assertEqual((r['who'], r['ok'], r['needs_review']), ('local', True, True))

    def test_local_failure_escalates_for_summaries(self):
        r = self.go({'codex': {'ok': True}, 'claude': {'ok': True}, 'local': {'ok': True}},
                    {'local': lambda *a: result(False), 'codex': lambda *a: result()}, kind='summarize')
        self.assertEqual(r['who'], 'codex')
        self.assertFalse(r['needs_review'])

    def test_ordinary_cloud_failure_is_not_retried(self):
        calls = []
        r = self.go({'codex': {'ok': True}, 'claude': {'ok': True}, 'local': {'ok': True}},
                    {'codex': lambda *a: calls.append('codex') or result(False), 'claude': lambda *a: calls.append('claude') or result()})
        self.assertEqual((calls, r['ok']), (['codex'], False))

    def test_nested_run_refused(self):
        os.environ['HUB_DEPTH'] = '1'
        with self.assertRaises(SystemExit):
            hub.run('task', kind='implement')

    def test_keywords(self):
        self.assertEqual(hub.classify('로그인 API 단위 테스트 작성'), 'test')
        self.assertEqual(hub.classify('모듈 구조 검토해줘'), 'review')
        self.assertEqual(hub.classify('이 로그 요약해줘'), 'summarize')
        self.assertEqual(hub.classify('결제 모듈 리팩터링'), 'design')

    def test_open_skips_exhausted_tools(self):
        quota = {'claude': {'ok': False}, 'codex': {'ok': True}, 'local': {'ok': True}}
        with mock.patch.object(hub, 'status', lambda fresh=False, names=(): {n: quota[n] for n in names}), \
                mock.patch.object(hub.os, 'execvpe') as execvpe:
            hub.open_tui('첫 요청')
        self.assertEqual(execvpe.call_args[0][1], ['codex', '첫 요청'])
        self.assertNotIn('HUB_DEPTH', execvpe.call_args[0][2])

    def test_review_goes_to_the_other_model(self):
        seen = []
        quota = {'codex': {'ok': False}, 'claude': {'ok': True}, 'local': {'ok': True}}
        with mock.patch.object(hub, 'is_git', lambda cwd: True), mock.patch.dict(os.environ, {'CLAUDECODE': '1'}), \
                mock.patch.object(hub, 'status', lambda: quota), \
                mock.patch.dict(hub.EXECUTORS, {'claude': lambda task, *a: seen.append(task) or result()}):
            report = hub.review(cwd=self.tmp.name)
        self.assertEqual(report['tried'][0], {'who': 'codex', 'skipped': 'quota'})
        self.assertEqual(report['who'], 'claude')
        self.assertIn('파일은 고치지 않는다', seen[-1])

    def test_review_needs_paths_for_large_dirty_trees(self):
        dirty = {f' M f{i}.py' for i in range(hub.REVIEW_LIMIT + 1)}
        with mock.patch.object(hub, 'is_git', lambda cwd: True), mock.patch.object(hub, 'git_status', lambda cwd: dirty):
            with self.assertRaises(SystemExit):
                hub.review(cwd=self.tmp.name)
            with mock.patch.object(hub, 'run', lambda task, *a, **k: task):
                self.assertIn("git diff HEAD -- a.py 'b c.py'", hub.review(['a.py', 'b c.py'], cwd=self.tmp.name))

    def test_open_print_quotes_for_eval(self):
        quota = {'claude': {'ok': True}}
        with mock.patch.object(hub, 'status', lambda fresh=False, names=(): {n: quota[n] for n in names}), \
                mock.patch('builtins.print') as out, mock.patch.object(hub.os, 'execvpe') as execvpe:
            hub.open_tui("it's", print_only=True)
        self.assertEqual(out.call_args_list[-1][0][0], "claude 'it'\"'\"'s'")
        execvpe.assert_not_called()


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dirs = {t: root / t for t in hub.TOOLS}
        for d in self.dirs.values():
            d.mkdir()
        self.source = root / 'src' / 'demo'
        self.source.mkdir(parents=True)
        (self.source / 'SKILL.md').write_text('---\nname: demo\n---\n')
        for p in (mock.patch.object(hub, 'SKILL_DIRS', {t: (d,) for t, d in self.dirs.items()}),
                  mock.patch.object(hub, 'LINK_DIR', dict(self.dirs))):
            p.start()
            self.addCleanup(p.stop)

    def test_links_then_recognizes_own_links(self):
        spec = {'skills': {'demo': {'all': str(self.source)}}}
        links, problems = hub.skill_plan(spec)
        self.assertEqual((len(links), problems), (3, []))
        for link, target in links.items():
            Path(link).symlink_to(target)
        again, problems = hub.skill_plan(spec)
        self.assertEqual((again, problems), (links, []))

    def test_other_copy_is_reported_not_linked(self):
        (self.dirs['codex'] / 'demo').mkdir()
        (self.dirs['codex'] / 'demo' / 'SKILL.md').write_text('copy')
        links, problems = hub.skill_plan({'skills': {'demo': {'codex': str(self.source)}}})
        self.assertEqual(links, {})
        self.assertIn('다른 사본', problems[0])

    def test_mcp_plan(self):
        spec = {'mcp': {'a': {'tools': ['codex'], 'url': 'https://a'},
                        'b': {'tools': ['claude'], 'command': 'x', 'args': ['1']}}}
        current = {'claude': {'b': {'type': 'stdio', 'command': 'x', 'args': ['2'], 'env': {'K': 'secret'}}},
                   'codex': {'a': {'url': 'https://a'}, 'old': {'url': 'https://old'}}, 'hermes': {}}
        commands, problems, wanted = hub.mcp_plan(spec, current, [['codex', 'old']])
        self.assertEqual(commands, [['codex', 'mcp', 'remove', 'old']])
        self.assertIn('env', problems[0])
        self.assertEqual(wanted, [('claude', 'b'), ('codex', 'a')])


if __name__ == '__main__':
    unittest.main()
