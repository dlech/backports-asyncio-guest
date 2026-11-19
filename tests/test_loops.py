"""Tests for the event loops that backports.asyncio.guest drives."""

import signal
import socket
import sys
import threading
import time
import unittest
from unittest import mock

from backports.asyncio.guest import _loops


def new_guest_loop(test):
    # On Windows, creating a ProactorEventLoop on the main thread installs its
    # self-pipe as the signal wakeup fd, and closing it resets the fd to -1:
    # restore the runner's.
    if threading.current_thread() is threading.main_thread():
        fd = signal.set_wakeup_fd(-1)
        signal.set_wakeup_fd(fd)
        test.addCleanup(signal.set_wakeup_fd, fd)
    loop = _loops.new_guest_loop()
    test.addCleanup(loop.close)
    return loop


class GuestLoopTests(unittest.TestCase):

    def setUp(self):
        self.loop = new_guest_loop(self)

    def test_steps_exist(self):
        for name in ('_poll_timeout', '_poll_events', 'process_events',
                     'process_ready', '_run_forever_setup',
                     '_run_forever_cleanup'):
            self.assertTrue(callable(getattr(self.loop, name)), name)

    def test_poll_timeout(self):
        self.assertIsNone(self.loop._poll_timeout())
        handle = self.loop.call_later(10, lambda: None)
        self.assertGreater(self.loop._poll_timeout(), 9)
        handle.cancel()
        self.loop.call_soon(lambda: None)
        self.assertEqual(self.loop._poll_timeout(), 0)

    def test_process_ready_runs_due_callbacks(self):
        calls = []
        self.loop.call_soon(calls.append, 'soon')
        self.loop.call_later(3600, calls.append, 'later')
        self.loop.process_ready()
        self.assertEqual(calls, ['soon'])

    def test_run_until_complete_still_works(self):
        # The guest's graceful shutdown drives the loop the ordinary way.
        async def coro():
            return 'ok'

        self.assertEqual(self.loop.run_until_complete(coro()), 'ok')


class WakeupFdTests(unittest.TestCase):

    def test_new_guest_loop_restores_wakeup_fd(self):
        # The tests' helper leaves the runner's signal wakeup fd as it was.
        if threading.current_thread() is not threading.main_thread():
            self.skipTest('signal.set_wakeup_fd() needs the main thread')
        before = signal.set_wakeup_fd(-1)
        signal.set_wakeup_fd(before)

        class Inner(unittest.TestCase):
            def runTest(inner):
                new_guest_loop(inner)

        result = unittest.TestResult()
        Inner().run(result)  # runs the cleanups: closes the loop
        self.assertTrue(result.wasSuccessful(), result.errors)
        after = signal.set_wakeup_fd(before)
        self.assertEqual(after, before)


@unittest.skipUnless(sys.platform == 'win32', 'IOCP only')
class GuestProactorTests(unittest.TestCase):

    def setUp(self):
        self.loop = new_guest_loop(self)

    def test_poll_events_only_waits(self):
        # _poll_events() doesn't process completions: process_events()
        # does, on the loop's thread.
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)

        fut = self.loop._proactor.recv(a, 100)
        b.send(b'data')
        statuses = []
        deadline = time.monotonic() + 10
        while not statuses and time.monotonic() < deadline:
            statuses = self.loop._poll_events(0.1)
        self.assertTrue(statuses)
        self.assertFalse(fut.done())

        self.loop.process_events(statuses)
        self.assertEqual(fut.result(), b'data')

    def test_wait_keeps_statuses_on_error(self):
        # Statuses already taken off the port aren't lost when a later
        # GetQueuedCompletionStatus() fails: the error carries them.
        import _overlapped

        proactor = self.loop._proactor
        status = (0, 4, 0, 0x1234)

        error = OSError(22, 'invalid')
        with mock.patch.object(_overlapped, 'GetQueuedCompletionStatus',
                               side_effect=[status, error]):
            with self.assertRaises(OSError) as cm:
                proactor._wait_for_completion_statuses(0)
        self.assertIs(cm.exception, error)
        self.assertEqual(_loops._events_from(error), [status])

        error = OSError(22, 'invalid')
        with mock.patch.object(_overlapped, 'GetQueuedCompletionStatus',
                               side_effect=error):
            with self.assertRaises(OSError):
                proactor._wait_for_completion_statuses(0)
        self.assertFalse(_loops._events_from(error))


if __name__ == '__main__':
    unittest.main()
