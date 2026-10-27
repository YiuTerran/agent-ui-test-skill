"""User-selected expansion scope is independent of existing regression eligibility."""
import copy
from unittest import TestCase

import test_tools as base

wf = base.wf


class ExpansionChoiceTest(TestCase):
    setUp = base.ToolsTest.setUp
    tearDown = base.ToolsTest.tearDown
    write = base.ToolsTest.write
    cli = base.ToolsTest.cli
    start = base.ToolsTest.start
    record = base.ToolsTest.record
    check_sources = base.ToolsTest.check_sources
    state = base.ToolsTest.state
    execution = base.ToolsTest.execution
    inventory = base.ToolsTest.inventory
    ignored = base.ToolsTest.ignored
    init_attempt = base.ToolsTest.init_attempt

    def begin(self, gaps=('C1',)):
        cov = wf.load(self.project / 'test/plans/coverage.json')
        for cid in gaps:
            cov['cases'][cid]['implementation'] = 'pending'
        self.write('test/plans/coverage.json', cov)
        self.start()
        return self.check_sources()

    def choose(self, mode='all', scopes=None, gaps=('C1',), **extra):
        if scopes is None:
            scopes = [] if mode == 'skip' else ['a', 'b']
        return self.record(dict(
            type='automation_expansion', owner=self.owner, mode=mode, scopes=scopes,
            gap_case_ids=list(gaps), user_reply='Synthetic explicit scope choice.',
            evidence=['test/evidence.txt'], **extra))

    def task(self, cid, phase='code', status='running', fail=False):
        return self.record({'type': 'task', 'task_id': phase + '-' + cid,
                            'phase': phase, 'status': status, 'owner': self.owner,
                            'case_ids': [cid]}, fail=fail)

    def test_gap_inventory_requests_selection_while_stable_case_is_ready(self):
        view = self.begin()
        choice = view['automation_expansion']
        self.assertEqual(1, choice['gap_count'])
        self.assertEqual({'a': ['C1']}, choice['modules'])
        self.assertEqual(['C1'], choice['pending_case_ids'])
        self.assertEqual(['modules', 'all', 'skip'], choice['choices'])
        self.assertIsNone(choice['decision'])
        self.assertIn('ask_user_to_choose_automation_expansion_scope', view['next_steps'])
        self.assertNotIn('fill_only_mapping_review_or_code_gaps', view['next_steps'])
        self.assertEqual(['C2'], view['regression_ready'])

    def test_exploration_and_code_tasks_require_choice_before_start(self):
        self.begin()
        for phase in ('exploration', 'code'):
            for status in ('running', 'approved', 'complete'):
                with self.subTest(phase=phase, status=status):
                    failure = self.task('C1', phase, status, fail=True)
                    self.assertIn('user automation expansion choice required', failure['error'])
        self.task('C1', status='pending')
        self.assertNotIn('automation_expansion', self.state())

    def test_missing_choice_rejects_added_case_code_and_coverage_update(self):
        self.begin()
        case = dict(wf.assets(self.project)[1]['cases']['C1'], implementation='valid')
        for extra in ({'code_action': 'added'}, {'code': 'complete'},
                      {'exploration': 'running'}, {'coverage': case}):
            event = dict(type='case', case_id='C1', action='updated', **extra)
            failure = self.record(event, fail=True)
            self.assertIn('user automation expansion choice required', failure['error'])
        self.assertEqual('pending', wf.assets(self.project)[1]['cases']['C1']['implementation'])
        self.assertEqual({}, self.state()['case_actions'])

    def test_module_choice_authorizes_only_selected_modules(self):
        self.begin(('C1', 'C2'))
        view = self.choose('modules', ['a'], ('C1', 'C2'))
        self.assertEqual(['C1'], view['automation_expansion']['approved_case_ids'])
        self.assertEqual(['C2'], view['automation_expansion']['skipped_case_ids'])
        self.assertEqual([], view['automation_expansion']['pending_case_ids'])
        self.task('C1')
        self.assertIn('choice required', self.task('C2', fail=True)['error'])

    def test_all_choice_authorizes_the_presented_cases(self):
        self.begin(('C1', 'C2'))
        self.choose(gaps=('C1', 'C2'))
        self.task('C1', 'exploration')
        self.task('C2', 'code')
        self.assertEqual('all', self.state()['automation_expansion']['mode'])

    def test_skip_preserves_coverage_and_allows_existing_regression(self):
        self.begin()
        before = (self.project / 'test/plans/coverage.json').read_bytes()
        view = self.choose('skip')
        self.assertEqual(['C1'], view['automation_expansion']['skipped_case_ids'])
        self.assertEqual([], view['automation_expansion']['pending_case_ids'])
        self.assertNotIn('ask_user_to_choose_automation_expansion_scope', view['next_steps'])
        self.assertNotIn('fill_only_mapping_review_or_code_gaps', view['next_steps'])
        self.record(self.execution('E2', 'C2'))
        self.assertEqual(before, (self.project / 'test/plans/coverage.json').read_bytes())
        self.assertIn('choice required', self.task('C1', fail=True)['error'])
        self.assertEqual('pending', wf.assets(self.project)[1]['cases']['C1']['implementation'])

    def test_skip_can_prepare_full_regression_of_all_existing_targets(self):
        self.begin()
        self.choose('skip')
        raw = self.inventory()
        raw['suites'][0]['specs'].pop(0)
        self.write('discovery.json', raw)
        self.write('preflight.json', {'discovery': {'path': 'discovery.json', 'complete': True,
                                                  'setup_projects': ['auth']}})
        with self.ignored():
            self.cli('preflight', '--run', self.run, '--owner', self.owner,
                     '--input', str(self.project / 'preflight.json'))
        result = self.init_attempt(regression_mode='full', scope=['a', 'b'])
        self.assertTrue(result['created'])
        self.assertEqual(['C1'], self.state()['automation_expansion']['gap_case_ids'])

    def test_new_gap_outside_approved_list_requires_another_choice(self):
        self.begin(('C1', 'C2'))
        self.choose(gaps=('C1',))
        view = self.cli('status', '--run', self.run)
        self.assertEqual(['C2'], view['automation_expansion']['pending_case_ids'])
        self.assertIn('choice required', self.task('C2', fail=True)['error'])
        self.choose(gaps=('C1', 'C2'))
        self.task('C2')

    def test_new_candidate_can_be_approved_before_it_exists_in_coverage(self):
        self.begin()
        self.choose('modules', ['a'], ('C1', 'C3'))
        cov = wf.assets(self.project)[1]
        case = dict(cov['cases']['C1'], test_id='C3', title='Synthetic C3', implementation='valid',
                    spec='test/src/C3.spec.ts')
        requirement = dict(cov['requirements']['R1'], case_ids=['C1', 'C3'])
        self.write('test/src/C3.spec.ts', '// Synthetic candidate')
        self.record({'type': 'case', 'case_id': 'C3', 'action': 'added', 'code_action': 'added',
                     'code': 'complete', 'coverage': case, 'requirements': {'R1': requirement}})
        self.assertIn('C3', wf.assets(self.project)[1]['cases'])

    def test_scope_choice_requires_user_reply_evidence_and_main_owner(self):
        self.begin()
        event = {'type': 'automation_expansion', 'owner': self.owner, 'mode': 'all',
                 'scopes': ['a', 'b'], 'gap_case_ids': ['C1'],
                 'user_reply': 'Synthetic explicit approval.', 'evidence': ['test/evidence.txt']}
        for field, bad_value in (('user_reply', ''), ('evidence', []), ('owner', 'worker')):
            with self.subTest(field=field):
                failure = self.record(dict(event, **{field: bad_value}), fail=True)
                self.assertTrue('reply' in failure['error'] or 'owner' in failure['error'])
        self.record(event, fail=True, writer='worker')
        self.assertNotIn('automation_expansion', self.state())

    def test_modes_have_explicit_scope_semantics(self):
        self.begin()
        for mode, scopes in (('all', ['a']), ('skip', ['a']), ('modules', []),
                             ('modules', ['outside']), ('default', ['a'])):
            with self.subTest(mode=mode, scopes=scopes):
                event = {'type': 'automation_expansion', 'owner': self.owner, 'mode': mode,
                         'scopes': scopes, 'gap_case_ids': ['C1'],
                         'user_reply': 'Synthetic', 'evidence': ['test/evidence.txt']}
                self.record(event, fail=True)
        self.assertNotIn('automation_expansion', self.state())

    def test_choice_is_not_inherited_by_a_new_round(self):
        self.begin()
        self.choose('skip')
        old_run = self.run
        self.record({'type': 'acceptance', 'owner': self.owner, 'status': 'partially_blocked'})
        view = self.start('--new-round')
        self.assertNotEqual(old_run, self.run)
        self.assertIsNone(view['automation_expansion']['decision'])
        self.assertEqual(['C1'], view['automation_expansion']['pending_case_ids'])

    def test_same_round_resume_retains_explicit_scope(self):
        self.begin()
        self.choose('modules', ['a'])
        self.start()
        self.assertEqual('modules', self.state()['automation_expansion']['mode'])
        self.assertEqual(['C1'], self.state()['automation_expansion']['gap_case_ids'])

    def test_discovery_missing_runner_target_requests_choice_even_if_files_exist(self):
        self.start()
        self.check_sources()
        raw = self.inventory()
        raw['suites'][0]['specs'].pop(0)
        self.write('discovery.json', raw)
        self.write('preflight.json', {'discovery': {'path': 'discovery.json', 'complete': True,
                                                  'setup_projects': []}})
        with self.ignored():
            result = self.cli('preflight', '--run', self.run, '--owner', self.owner,
                              '--input', str(self.project / 'preflight.json'))
        self.assertEqual(['C1'], result['run']['automation_expansion']['pending_case_ids'])
        self.assertIn('ask_user_to_choose_automation_expansion_scope', result['next_steps'])
        self.assertIn('choice required', self.task('C1', fail=True)['error'])

    def test_completed_authorized_case_leaves_the_gap_inventory(self):
        self.begin()
        self.choose('modules', ['a'])
        cov = wf.assets(self.project)[1]
        case = dict(cov['cases']['C1'], implementation='valid')
        view = self.record({'type': 'case', 'case_id': 'C1', 'action': 'updated', 'code': 'complete',
                            'code_action': 'added', 'coverage': case})
        self.assertEqual(0, view['automation_expansion']['gap_count'])
        self.assertEqual(['C1', 'C2'], view['regression_ready'])
        self.assertEqual('modules', view['automation_expansion']['decision']['mode'])

    def test_stable_repair_and_regression_do_not_request_expansion(self):
        self.start()
        view = self.check_sources()
        self.assertEqual(0, view['automation_expansion']['gap_count'])
        self.assertNotIn('ask_user_to_choose_automation_expansion_scope', view['next_steps'])
        self.record({'type': 'case', 'case_id': 'C1', 'action': 'updated',
                     'code': 'complete', 'code_action': 'modified'})
        self.record(self.execution('E1'))

    def test_summary_exposes_the_user_choice_and_remaining_gap(self):
        self.begin(('C1', 'C2'))
        self.choose('modules', ['a'], ('C1', 'C2'))
        state = self.state()
        spec, cov = wf.assets(self.project)
        result = wf.summary(self.project, copy.deepcopy(state), spec, cov)
        self.assertEqual(['C2'], result['automation_expansion']['skipped_case_ids'])
        self.assertEqual('modules', result['automation_expansion']['decision']['mode'])
