import io
import os
import signal
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
import threading
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from .matching_runner import (DRAIN_SECONDS, HEARTBEAT_MAX_AGE, POLL_SECONDS, PROBATION_SECONDS, STOP, STOP_DEADLINE_SECONDS, Heartbeat, Stop,
                              WorkerStopping, checkpoint, healthcheck, main, no_cut, run)
from .models import User
from .push import SENDER, dispatch, drain


class StopLoop(Exception):
    pass


class RunnerTestMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'matching.heartbeat')

    def age(self, seconds):
        os.utime(self.path, (time.time() - seconds,) * 2)


class MatchingRunnerLoopTests(RunnerTestMixin, SimpleTestCase):
    def loop(self, process_queue, sleeps, close_error=None, **kwargs):
        delays, stderr, stdout = [], io.StringIO(), io.StringIO()
        def sleep(delay):
            delays.append((delay, os.path.exists(self.path)))
            if len(delays) == sleeps:
                raise StopLoop
        with patch('django.db.connections.close_all', side_effect=close_error) as close_all, redirect_stderr(stderr), \
                redirect_stdout(stdout), self.assertRaises(StopLoop):
            run(process_queue, heartbeat=Heartbeat(self.path, interval=0), sleep=sleep, **kwargs)
        return delays, stderr.getvalue(), stdout.getvalue(), close_all

    def test_failed_pass_is_logged_with_traceback_and_the_loop_keeps_running(self):
        process_queue = Mock(side_effect=[RuntimeError('database exploded'), {'matched_items': 1}])
        delays, stderr, stdout, close_all = self.loop(process_queue, 2, force=True)
        self.assertIn('Matching pass failed (1 in a row); retrying in 5s.', stderr)
        self.assertIn('Traceback (most recent call last)', stderr)
        self.assertIn('RuntimeError: database exploded', stderr)
        self.assertIn("{'matched_items': 1}", stdout)
        # The failed pass drops its connection and does not refresh liveness; the next success does.
        close_all.assert_called_once()
        self.assertEqual(delays, [(5, False), (5, True)])
        self.assertEqual([c.kwargs for c in process_queue.call_args_list],
                         [{'force': True, 'use_ai': True}, {'force': False, 'use_ai': True}])

    def test_repeated_failures_back_off_up_to_a_cap_and_reset_after_success(self):
        outcomes = [RuntimeError(i) for i in range(6)] + [None, RuntimeError('again')]
        delays, stderr, _, close_all = self.loop(Mock(side_effect=outcomes), 8, use_ai=False)
        self.assertEqual([delay for delay, _ in delays], [5, 10, 20, 40, 60, 60, 5, 5])
        self.assertIn('(6 in a row); retrying in 60s.', stderr)
        self.assertIn('(1 in a row); retrying in 5s.', stderr.split('(6 in a row)')[1])
        self.assertEqual(close_all.call_count, 7)

    def test_connection_reset_failure_does_not_stop_the_loop(self):
        delays, stderr, _, close_all = self.loop(Mock(side_effect=[RuntimeError('boom'), None]), 2,
                                                 close_error=RuntimeError('close failed'))
        close_all.assert_called_once()
        self.assertIn('RuntimeError: close failed', stderr)
        self.assertEqual(delays, [(5, False), (5, True)])

    def test_once_still_raises_and_returns_after_one_success(self):
        sleep = Mock(side_effect=AssertionError('--once must not sleep'))
        with self.assertRaisesMessage(RuntimeError, 'boom'):
            run(Mock(side_effect=RuntimeError('boom')), once=True, heartbeat=Heartbeat(self.path), sleep=sleep)
        self.assertFalse(os.path.exists(self.path))
        process_queue = Mock(return_value=None)
        run(process_queue, once=True, force=True, use_ai=False, sleep=sleep)
        process_queue.assert_called_once_with(force=True, use_ai=False)

    def test_main_wires_options_and_keeps_manual_passes_off_the_heartbeat(self):
        with patch('django.setup'), patch('mall.matching_worker.process_queue') as process_queue, \
                patch('mall.matching_runner.run') as runner, patch.dict(os.environ, {'MATCHING_HEARTBEAT_FILE': self.path}):
            main(['--once', '--force', '--no-ai'])
            main([])
        once, loop = runner.call_args_list
        self.assertEqual((once.args, once.kwargs['once'], once.kwargs['force'], once.kwargs['use_ai']),
                         ((process_queue,), True, True, False))
        self.assertEqual((once.kwargs['heartbeat'].path, loop.kwargs['heartbeat'].path), ('', self.path))


class MatchingHeartbeatTests(RunnerTestMixin, SimpleTestCase):
    def test_beats_are_throttled_by_the_monotonic_clock(self):
        now = [100.0]
        heartbeat = Heartbeat(self.path, interval=5, clock=lambda: now[0])
        heartbeat.beat()
        self.age(1000)
        now[0] = 104
        heartbeat.beat()
        self.assertGreater(time.time() - os.stat(self.path).st_mtime, 900)
        now[0] = 105
        heartbeat.beat()
        self.assertLess(time.time() - os.stat(self.path).st_mtime, 60)

    def test_unwritable_heartbeat_never_raises_and_warns_once(self):
        heartbeat, stderr = Heartbeat(os.path.join(self.tmp.name, 'missing', 'heartbeat'), interval=0), io.StringIO()
        with redirect_stderr(stderr):
            heartbeat.beat()
            heartbeat.beat()
        self.assertEqual(stderr.getvalue().count('Matching heartbeat not written'), 1)

    def test_only_completed_queries_beat(self):
        heartbeat = Heartbeat(self.path, interval=0)
        with self.assertRaises(RuntimeError):
            heartbeat.on_query(Mock(side_effect=RuntimeError), 'SELECT 1', None, False, {})
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(heartbeat.on_query(Mock(return_value=7), 'SELECT 1', None, False, {}), 7)
        self.assertTrue(os.path.exists(self.path))

    def test_healthcheck_requires_a_fresh_heartbeat(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(healthcheck(self.path), 1)
            Heartbeat(self.path).beat()
            self.assertEqual(healthcheck(self.path), 0)
            self.age(HEARTBEAT_MAX_AGE - 30)
            self.assertEqual(healthcheck(self.path), 0)
            self.age(HEARTBEAT_MAX_AGE + 30)
            self.assertEqual(healthcheck(self.path), 1)
            self.assertEqual(healthcheck(self.path, max_age=HEARTBEAT_MAX_AGE + 60), 0)
        self.assertIn('No matching heartbeat at', out.getvalue())
        self.assertIn(f'old (limit {HEARTBEAT_MAX_AGE}s)', out.getvalue())

    def test_healthcheck_command_reads_its_configuration_from_the_environment(self):
        Heartbeat(self.path).beat()
        self.age(60)
        with redirect_stdout(io.StringIO()), patch.dict(os.environ, {'MATCHING_HEARTBEAT_FILE': self.path}):
            for max_age, code in [('90', 0), ('30', 1)]:
                with patch.dict(os.environ, {'MATCHING_HEARTBEAT_MAX_AGE': max_age}), self.assertRaises(SystemExit) as exit:
                    main(['--healthcheck'])
                self.assertEqual(exit.exception.code, code)


class MatchingRunnerQueryHeartbeatTests(RunnerTestMixin, TestCase):
    def test_completed_queries_beat_inside_a_long_pass(self):
        def long_pass(**kwargs):
            self.assertFalse(os.path.exists(self.path))
            User.objects.count()
            self.assertTrue(os.path.exists(self.path))
        with patch('django.db.close_old_connections'):
            run(long_pass, once=True, heartbeat=Heartbeat(self.path))

    def test_failure_inside_a_leaked_transaction_exits_instead_of_spinning(self):
        # TestCase holds an atomic block open, as code that leaked one would.
        with patch('django.db.close_old_connections'), redirect_stderr(io.StringIO()), \
                self.assertRaisesMessage(RuntimeError, 'boom'):
            run(Mock(side_effect=RuntimeError('boom')), sleep=Mock(side_effect=AssertionError('must not retry')))


class MatchingRunnerFailingPassTests(RunnerTestMixin, TransactionTestCase):
    def test_passes_that_keep_failing_after_queries_go_stale_until_a_pass_outlives_its_probation(self):
        now, seen = [1000.0], []
        def failing_pass(**kwargs):
            User.objects.count()  # e.g. the advisory lock and import checks succeed, then the lease query fails
            raise RuntimeError('column does not exist')
        def long_pass(**kwargs):
            User.objects.count()
            seen.append(os.path.exists(self.path))
            now[0] += PROBATION_SECONDS
            User.objects.count()
            seen.append(os.path.exists(self.path))
        def sleep(delay):
            seen.append(os.path.exists(self.path))
            if os.path.exists(self.path):
                os.remove(self.path)
            now[0] += delay
            if len(seen) == 5:
                raise StopLoop
        passes = iter([failing_pass, failing_pass, long_pass])
        with patch('django.db.connections.close_all'), redirect_stderr(io.StringIO()), self.assertRaises(StopLoop):
            run(lambda **kwargs: next(passes)(**kwargs), heartbeat=Heartbeat(self.path, interval=0, clock=lambda: now[0]), sleep=sleep)
        # First failure still beats; repeats do not; a long pass after them beats once past its probation.
        self.assertEqual(seen, [True, False, False, True, True])


class RunnerFreezeAndStopTests(RunnerTestMixin, SimpleTestCase):
    """The loop under the catalog freeze (mall.maintenance) and after SIGTERM/SIGINT."""

    def setUp(self):
        super().setUp()
        self.folder = os.path.join(self.tmp.name, 'maintenance')
        os.mkdir(self.folder)
        settings_override = override_settings(MAINTENANCE_DIR=self.folder)
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        STOP.reset()
        self.addCleanup(STOP.reset)

    def freeze(self, level):
        path = os.path.join(self.folder, 'freeze')
        if level is None:
            os.remove(path)
        else:
            with open(path, 'w') as handle:
                handle.write(level)

    def loop(self, process_queue, sleep, **kwargs):
        stdout = io.StringIO()
        with patch('django.db.connections.close_all'), redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            run(process_queue, heartbeat=Heartbeat(self.path, interval=0), sleep=sleep, **kwargs)
        return stdout.getvalue()

    def test_the_freeze_idles_the_loop_with_a_live_heartbeat_until_it_is_lifted(self):
        self.freeze('catalog')
        delays, process_queue = [], Mock(return_value=None)
        def sleep(delay):
            delays.append((delay, process_queue.call_count, os.path.exists(self.path)))
            if len(delays) == 2:
                self.freeze(None)
            if len(delays) == 3:
                STOP.handle(signal.SIGTERM)
        out = self.loop(process_queue, sleep)
        self.assertEqual(delays, [(POLL_SECONDS, 0, True), (POLL_SECONDS, 0, True), (POLL_SECONDS, 1, True)])
        self.assertEqual(out.count('Maintenance freeze (catalog): idle until it is lifted.'), 1)
        self.assertIn('Maintenance freeze lifted: resuming.', out)
        self.assertIn('SIGTERM: exiting.', out)

    def test_quote_holds_keep_running_under_the_catalog_freeze_and_idle_at_level_all(self):
        for level, passes in [('catalog', 1), ('all', 0)]:
            self.freeze(level)
            process_queue = Mock(return_value=None)
            with redirect_stdout(io.StringIO()):
                run(process_queue, once=True, idle_under=('all',), sleep=Mock(side_effect=AssertionError('--once must not sleep')))
            self.assertEqual(process_queue.call_count, passes, level)

    def test_a_manual_pass_does_not_run_under_the_freeze(self):
        self.freeze('all')
        process_queue = Mock(return_value=None)
        with redirect_stdout(io.StringIO()) as out:
            run(process_queue, once=True, sleep=Mock(side_effect=AssertionError('--once must not sleep')))
        process_queue.assert_not_called()
        self.assertIn('Maintenance freeze (all): no pass while it is on.', out.getvalue())

    def test_a_signal_while_waiting_exits_before_another_pass(self):
        process_queue = Mock(return_value=None)
        out = self.loop(process_queue, lambda delay: STOP.handle(signal.SIGINT))
        process_queue.assert_called_once()
        self.assertIn('SIGINT: exiting.', out)

    def test_a_signal_during_a_pass_stops_it_at_its_next_checkpoint_only_once(self):
        steps = []
        def process_queue(**kwargs):
            checkpoint()  # nothing asked yet: goes on
            steps.append('unit 1')
            STOP.handle(signal.SIGTERM)
            try:
                checkpoint()
            except WorkerStopping as stopped:
                steps.append(str(stopped))
                checkpoint()  # the code giving the lease back runs past it
                steps.append('lease given back')
                raise
            steps.append('unit 2')
        out = self.loop(process_queue, Mock(side_effect=AssertionError('must exit, not sleep')))
        self.assertEqual(steps, ['unit 1', 'signal', 'lease given back'])
        self.assertIn('Pass stopped between units (SIGTERM).', out)
        self.assertIn('SIGTERM: exiting.', out)

    def test_a_freeze_that_begins_during_a_pass_idles_the_loop_instead_of_exiting(self):
        passes = []
        def process_queue(**kwargs):
            passes.append(len(passes))
            if len(passes) == 1:
                self.freeze('catalog')
                checkpoint()
        def sleep(delay):
            if len(passes) == 1:
                self.freeze(None)
            else:
                STOP.handle(signal.SIGTERM)
        out = self.loop(process_queue, sleep)
        self.assertEqual(passes, [0, 1])
        self.assertIn('Pass stopped between units (maintenance freeze).', out)
        self.assertIn('Maintenance freeze (catalog): idle until it is lifted.', out)

    def test_handlers_are_installed_for_the_loop_and_restored_after_it(self):
        before = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        seen = []
        def process_queue(**kwargs):
            seen.append(signal.getsignal(signal.SIGTERM) == STOP.handle)
            signal.raise_signal(signal.SIGINT)  # with the default handler this would be a KeyboardInterrupt
            seen.append(STOP.requested)
        with patch('mall.push.drain', return_value=0) as drained:
            self.loop(process_queue, Mock(side_effect=AssertionError('must exit, not sleep')), signals=True)
        self.assertEqual(seen, [True, True])
        self.assertEqual({number: signal.getsignal(number) for number in before}, before)
        drained.assert_called_once_with(DRAIN_SECONDS)  # the notifications the last pass started may finish first

    def test_at_exit_the_notifications_still_sending_get_their_time(self):
        release = threading.Event()
        dispatch(release.wait)
        with redirect_stderr(io.StringIO()) as err, patch('mall.matching_runner.DRAIN_SECONDS', 0.05):
            run(Mock(return_value=None), once=True, signals=True, sleep=Mock())
        self.assertIn('1 notification send still running at exit.', err.getvalue())
        release.set()
        self.assertEqual(drain(5), 0)
        self.assertFalse(any(thread.name == SENDER for thread in threading.enumerate()))

    def test_a_second_signal_does_not_wait(self):
        STOP.handle(signal.SIGTERM)
        with self.assertRaises(SystemExit) as exit:
            STOP.handle(signal.SIGTERM)
        self.assertEqual(exit.exception.code, 128 + signal.SIGTERM)

    def test_the_default_sleep_is_cut_short_by_a_signal(self):
        STOP.handle(signal.SIGTERM)
        started = time.monotonic()
        STOP.sleep(30)
        self.assertLess(time.monotonic() - started, 1)


class StopDeadlineTests(SimpleTestCase):
    """A pass that reaches no checkpoint in time is cut at its next query outside a transaction, never inside one."""

    def test_after_the_deadline_the_next_query_outside_a_transaction_raises_once(self):
        now = [100.0]
        stop = Stop(clock=lambda: now[0])
        execute = Mock(return_value='rows')
        outside, inside = ({'connection': Mock(in_atomic_block=atomic, run_commit_hooks_on_set_autocommit_on=False)} for atomic in (False, True))
        self.assertEqual(stop.on_query(execute, 'SELECT 1', None, False, outside), 'rows')
        stop.handle(signal.SIGTERM)
        now[0] += STOP_DEADLINE_SECONDS - 1
        self.assertEqual(stop.on_query(execute, 'SELECT 1', None, False, outside), 'rows')  # still time to reach a checkpoint
        now[0] += 1
        self.assertEqual(stop.on_query(execute, 'UPDATE x', None, False, inside), 'rows')  # a unit already begun commits whole
        with self.assertRaises(WorkerStopping):
            stop.on_query(execute, 'SELECT 2', None, False, outside)
        self.assertEqual(stop.on_query(execute, 'UPDATE lease', None, False, outside), 'rows')  # giving the lease back still runs
        self.assertEqual([call.args[0] for call in execute.call_args_list], ['SELECT 1', 'SELECT 1', 'UPDATE x', 'UPDATE lease'])

    def test_commit_hooks_and_no_cut_blocks_are_never_cut(self):
        now = [100.0]
        stop = Stop(clock=lambda: now[0])
        execute = Mock(return_value='rows')
        hooks = {'connection': Mock(in_atomic_block=False, run_commit_hooks_on_set_autocommit_on=True)}
        outside = {'connection': Mock(in_atomic_block=False, run_commit_hooks_on_set_autocommit_on=False)}
        stop.handle(signal.SIGTERM)
        now[0] += STOP_DEADLINE_SECONDS + 1
        self.assertEqual(stop.on_query(execute, 'UPDATE queue', None, False, hooks), 'rows')  # an on_commit hook: the notice goes out
        with stop.no_cut(), stop.no_cut():
            self.assertEqual(stop.on_query(execute, 'UPDATE lease', None, False, outside), 'rows')
        with self.assertRaises(WorkerStopping):
            stop.on_query(execute, 'SELECT 2', None, False, outside)
        self.assertEqual(stop.shield, 0)

    def test_no_cut_still_lets_a_checkpoint_stop_the_pass(self):
        STOP.reset()
        self.addCleanup(STOP.reset)
        STOP.handle(signal.SIGTERM)
        with self.assertRaises(WorkerStopping), no_cut():
            checkpoint()
        self.assertEqual(STOP.shield, 0)

    def test_main_installs_the_handlers_for_the_service_and_manual_passes(self):
        with patch('django.setup'), patch('mall.matching_worker.process_queue'), patch('mall.matching_runner.run') as runner:
            main(['--once'])
            main([])
        self.assertEqual([call.kwargs['signals'] for call in runner.call_args_list], [True, True])
        from . import apiag_sync_runner, quote_holds_runner
        for module in (apiag_sync_runner, quote_holds_runner):
            with patch('django.setup'), patch(f'{module.__name__}.run') as runner:
                module.main([])
            self.assertTrue(runner.call_args.kwargs['signals'], module.__name__)
        self.assertEqual(runner.call_args.kwargs['idle_under'], ('all',))  # quote-holds


class StopDeadlineCommitHookTests(TransactionTestCase):
    """Django runs on_commit hooks after the outermost atomic block, outside it: the deadline cut waits until they are done."""

    def test_the_hooks_of_a_committed_unit_run_whole_and_the_next_query_is_cut(self):
        now = [100.0]
        stop = Stop(clock=lambda: now[0])
        stop.handle(signal.SIGTERM)
        hooks = []
        def notice(label):
            hooks.append((label, connection.in_atomic_block, User.objects.count()))
        with connection.execute_wrapper(stop.on_query):
            with transaction.atomic():
                User.objects.create_user('hook-user', 'hook-user@example.invalid', 'HookUser938!')
                transaction.on_commit(lambda: notice('push'))
                transaction.on_commit(lambda: notice('email'))
                now[0] += STOP_DEADLINE_SECONDS  # the deadline passes while the unit commits
            self.assertEqual(hooks, [('push', False, 1), ('email', False, 1)])
            with self.assertRaises(WorkerStopping):
                User.objects.count()
