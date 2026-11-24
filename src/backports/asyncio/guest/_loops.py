"""
Event loops that asyncio guest mode can drive one step at a time.

These are private subclasses of asyncio's own loops, created by
start_guest_run().  They add what CPython's guest mode branch adds to
BaseEventLoop and ProactorEventLoop (python/cpython#145343): the parts of
_run_once() as separate steps, and on Windows an IOCP wait that doesn't
process what it waits for.  The stock loops are not modified.

They are built from asyncio's private parts, as any loop driven from outside
has to be until Python ships guest mode, and are tested on each supported
Python version.
"""

import heapq
import math
import sys
import threading
import time

from asyncio import base_events
from asyncio import events
from asyncio.log import logger

import asyncio

__all__ = ('new_guest_loop',)

_MIN_SCHEDULED_TIMER_HANDLES = base_events._MIN_SCHEDULED_TIMER_HANDLES
_MIN_CANCELLED_TIMER_HANDLES_FRACTION = (
    base_events._MIN_CANCELLED_TIMER_HANDLES_FRACTION)
MAXIMUM_SELECT_TIMEOUT = base_events.MAXIMUM_SELECT_TIMEOUT
_format_handle = base_events._format_handle

# BaseEventLoop._clock_resolution until Python 3.16 made it
# asyncio.constants.CLOCK_RESOLUTION (gh-161032).
_CLOCK_RESOLUTION = time.get_clock_info('monotonic').resolution


# If a loop's _poll_events() fails after taking events, such as completion
# statuses off an IOCP port, the exception carries them, so the caller can
# still process them; else their futures would never complete.

def _attach_events(exc, event_list):
    exc._asyncio_events = event_list


def _events_from(exc):
    return getattr(exc, '_asyncio_events', ())


class _GuestLoopMixin:
    """The steps of BaseEventLoop._run_once(), callable one at a time."""

    _guest_mode = False

    if sys.version_info < (3, 13):
        # Python 3.13 split these out of run_forever() (gh-110771).

        _old_agen_hooks = None

        def _run_forever_setup(self):
            self._check_closed()
            self._check_running()
            self._set_coroutine_origin_tracking(self._debug)
            self._old_agen_hooks = sys.get_asyncgen_hooks()
            self._thread_id = threading.get_ident()
            sys.set_asyncgen_hooks(
                firstiter=self._asyncgen_firstiter_hook,
                finalizer=self._asyncgen_finalizer_hook)
            events._set_running_loop(self)

        def _run_forever_cleanup(self):
            self._stopping = False
            self._thread_id = None
            events._set_running_loop(None)
            self._set_coroutine_origin_tracking(False)
            if self._old_agen_hooks is not None:
                sys.set_asyncgen_hooks(*self._old_agen_hooks)
                self._old_agen_hooks = None

    # True while a guest run's own code runs on the loop's thread: set
    # by start_guest_run() around each batch, the start's guest code, the
    # processing of a failed poll's events and the unwind of a failed
    # start, and from the final cleanup on.
    _guest_code_running = False

    def _check_thread(self):
        # Debug mode only, like the method it extends.  In guest mode, the
        # loop runs for the whole guest run, but its own code runs only
        # while _guest_code_running is set.  At other times, on the loop's
        # own thread, the caller is host code, between batches: nothing
        # would wake the I/O thread's wait for the work it schedules.
        # (Guest code of another guest run that this loop hosts runs
        # while this loop's own code runs.)
        super()._check_thread()
        if (self._guest_mode
                and self._thread_id is not None
                and not self._guest_code_running):
            raise RuntimeError(
                "Non-thread-safe operation invoked on an asyncio guest loop "
                "from host code, between its batches: use "
                "loop.call_soon_threadsafe()")

    def _poll_timeout(self):
        """Return the timeout for the next poll, after removing cancelled
        timers: 0 if callbacks are ready, None if nothing is scheduled."""
        sched_count = len(self._scheduled)
        if (sched_count > _MIN_SCHEDULED_TIMER_HANDLES and
            self._timer_cancelled_count / sched_count >
                _MIN_CANCELLED_TIMER_HANDLES_FRACTION):
            # Remove delayed calls that were cancelled if their number
            # is too high
            new_scheduled = []
            for handle in self._scheduled:
                if handle._cancelled:
                    handle._scheduled = False
                else:
                    new_scheduled.append(handle)

            heapq.heapify(new_scheduled)
            self._scheduled = new_scheduled
            self._timer_cancelled_count = 0
        else:
            # Remove delayed calls that were cancelled from head of queue.
            while self._scheduled and self._scheduled[0]._cancelled:
                self._timer_cancelled_count -= 1
                handle = heapq.heappop(self._scheduled)
                handle._scheduled = False

        timeout = None
        if self._ready or self._stopping:
            timeout = 0
        elif self._scheduled:
            # Compute the desired timeout.
            timeout = self._scheduled[0]._when - self.time()
            if timeout > MAXIMUM_SELECT_TIMEOUT:
                timeout = MAXIMUM_SELECT_TIMEOUT
            elif timeout < 0:
                timeout = 0
        return timeout

    def _poll_events(self, timeout):
        # May run on a thread other than the loop's: it must only wait,
        # and leave everything that touches the loop to process_events().
        return self._selector.select(timeout)

    def process_events(self, event_list):
        self._process_events(event_list)

    def process_ready(self):
        # Handle 'later' callbacks that are ready.
        now = self.time()
        # Ensure that `end_time` is strictly increasing
        # when the clock resolution is too small.
        resolution = getattr(self, '_clock_resolution', _CLOCK_RESOLUTION)
        end_time = now + max(resolution, math.ulp(now))
        while self._scheduled:
            handle = self._scheduled[0]
            if handle._when >= end_time:
                break
            handle = heapq.heappop(self._scheduled)
            handle._scheduled = False
            self._ready.append(handle)

        # This is the only place where callbacks are actually *called*.
        # All other places just add them to ready.
        # Note: We run all currently scheduled callbacks, but not any
        # callbacks scheduled by callbacks run this time around --
        # they will be run the next time (after another I/O poll).
        # Use an idiom that is thread-safe without using locks.
        ntodo = len(self._ready)
        for i in range(ntodo):
            handle = self._ready.popleft()
            if handle._cancelled:
                continue
            if self._debug:
                try:
                    self._current_handle = handle
                    t0 = self.time()
                    handle._run()
                    dt = self.time() - t0
                    if dt >= self.slow_callback_duration:
                        logger.warning('Executing %s took %.3f seconds',
                                       _format_handle(handle), dt)
                finally:
                    self._current_handle = None
            else:
                handle._run()
        handle = None  # Needed to break cycles when an exception occurs.


class _GuestSelectorEventLoop(_GuestLoopMixin, asyncio.SelectorEventLoop):

    if sys.platform != 'win32':
        def add_signal_handler(self, sig, callback, *args):
            if self._guest_mode:
                raise RuntimeError(
                    "add_signal_handler() is not supported in asyncio guest "
                    "mode; the host event loop owns signal handling -- "
                    "forward signals to the loop with call_soon_threadsafe()")
            return super().add_signal_handler(sig, callback, *args)

        def remove_signal_handler(self, sig):
            if self._guest_mode:
                raise RuntimeError(
                    "remove_signal_handler() is not supported in asyncio "
                    "guest mode; the host event loop owns signal handling")
            return super().remove_signal_handler(sig)


if sys.platform == 'win32':
    import _overlapped
    import _winapi
    from asyncio import windows_events

    INFINITE = _winapi.INFINITE

    def _timeout_to_ms(timeout):
        if timeout is None:
            return INFINITE
        elif timeout < 0:
            raise ValueError("negative timeout")
        # GetQueuedCompletionStatus() has a resolution of 1 millisecond,
        # round away from zero to wait *at least* timeout seconds.
        ms = math.ceil(timeout * 1e3)
        if ms >= INFINITE:
            raise ValueError("timeout too big")
        return ms

    class _GuestIocpProactor(windows_events.IocpProactor):
        """An IOCP proactor whose wait for completions doesn't process
        them, so it may run on a thread other than the loop's."""

        def _process_completion_status(self, status):
            # The body of IocpProactor._poll()'s loop, which Python 3.16
            # splits out as this method (gh-154971).
            err, transferred, key, address = status
            try:
                f, ov, obj, callback = self._cache.pop(address)
            except KeyError:
                if self._loop.get_debug():
                    self._loop.call_exception_handler({
                        'message': ('GetQueuedCompletionStatus() returned an '
                                    'unexpected event'),
                        'status': ('err=%s transferred=%s key=%#x address=%#x'
                                   % (err, transferred, key, address)),
                    })

                # key is either zero, or it is used to return a pipe
                # handle which should be closed to avoid a leak.
                if key not in (0, _overlapped.INVALID_HANDLE_VALUE):
                    _winapi.CloseHandle(key)
                return

            if obj in self._stopped_serving:
                f.cancel()
            # Don't call the callback if _register() already read the result or
            # if the overlapped has been cancelled
            elif not f.done():
                try:
                    value = callback(transferred, key, ov)
                except OSError as e:
                    f.set_exception(e)
                    self._results.append(f)
                else:
                    f.set_result(value)
                    self._results.append(f)
                finally:
                    f = None

        def _wait_for_completion_statuses(self, timeout=None):
            """Wait for statuses from the completion port, and return them,
            without processing them: pass them to
            _process_completion_statuses() on the event loop's thread."""
            ms = _timeout_to_ms(timeout)
            statuses = []
            while True:
                try:
                    status = _overlapped.GetQueuedCompletionStatus(
                        self._iocp, ms)
                except OSError as e:
                    if statuses:
                        # The statuses already taken off the port go with
                        # the error, for the caller to process: else they're
                        # lost.
                        _attach_events(e, statuses)
                    raise
                if status is None:
                    return statuses
                ms = 0
                statuses.append(status)

        def _process_completion_statuses(self, statuses):
            """Process statuses from _wait_for_completion_statuses(), and
            return the futures they completed, as select() does."""
            for status in statuses:
                self._process_completion_status(status)
            for ov in self._unregistered:
                self._cache.pop(ov.address, None)
            self._unregistered.clear()
            tmp = self._results
            self._results = []
            try:
                return tmp
            finally:
                # Needed to break cycles when an exception occurs.
                tmp = None

    class _GuestProactorEventLoop(_GuestLoopMixin,
                                  asyncio.ProactorEventLoop):

        def __init__(self):
            super().__init__(_GuestIocpProactor())

        if sys.version_info < (3, 13):
            # ProactorEventLoop.run_forever() does this around the loop
            # until Python 3.13.

            def _run_forever_setup(self):
                assert self._self_reading_future is None
                self.call_soon(self._loop_self_reading)
                super()._run_forever_setup()

            def _run_forever_cleanup(self):
                super()._run_forever_cleanup()
                if self._self_reading_future is not None:
                    ov = self._self_reading_future._ov
                    self._self_reading_future.cancel()
                    # self_reading_future always uses IOCP, so even though
                    # it's been cancelled, we need to make sure that the
                    # IOCP message is received so that the kernel is not
                    # holding on to the memory, possibly causing memory
                    # corruption later. Only unregister it if IO is complete
                    # in all respects. Otherwise we need another _poll()
                    # later to complete the IO.
                    if ov is not None and not ov.pending:
                        self._proactor._unregister(ov)
                    self._self_reading_future = None

        def _poll_events(self, timeout):
            # Only waits, so it may run on another thread: processing a
            # completion runs its callback and completes its future, which
            # process_events() does, on the loop's thread.
            return self._proactor._wait_for_completion_statuses(timeout)

        def process_events(self, event_list):
            self._process_events(
                self._proactor._process_completion_statuses(event_list))

    def new_guest_loop():
        return _GuestProactorEventLoop()

else:
    def new_guest_loop():
        return _GuestSelectorEventLoop()
