import io
import os
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from .matching_runner import HEARTBEAT_MAX_AGE, PROBATION_SECONDS, Heartbeat, healthcheck, main, run
from .models import User


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
