# backports.asyncio.guest

A backport of `asyncio.start_guest_run()`, for Python 3.11 and later: it runs
asyncio as a guest inside another event loop that keeps control of its
thread, such as a GUI toolkit's main loop.

It follows CPython's guest mode proposal,
[python/cpython#145343](https://github.com/python/cpython/pull/145343), with
the Windows fixes in
[dlech/cpython@guest-mode-windows](https://github.com/dlech/cpython/tree/guest-mode-windows).
If a later Python ships `asyncio.start_guest_run()`, this package uses that
instead.

**Status:** not released. It will be published once CPython has merged guest
mode, with its API matched to what was merged (see [Releasing](#releasing)).

## How it works

Like [Trio's guest mode](https://trio.readthedocs.io/en/stable/reference-lowlevel.html#using-guest-mode-to-run-trio-on-top-of-other-event-loops),
the host only has to provide a thread-safe "run this soon" function.

- Your coroutines, and every asyncio callback, run on the host's thread, in
  batches the host runs for you.
- A helper thread only *waits* for I/O. When something is ready, or a timer is
  due, it asks the host to run the next batch.
- One token passes between the two threads, so the loop is never touched by
  both at once.
- Modal loops entered by the host, such as a window being dragged or a
  `MessageBox`, keep asyncio running, because they keep running the host's
  queue.

## Usage

```python
from backports.asyncio.guest import start_guest_run

task = start_guest_run(
    main,                                  # async function, then its args
    run_sync_soon_threadsafe=host_post,    # schedules fn() on the host thread
    done_callback=on_done,                 # called with the Task at the end
)
```

- `run_sync_soon_threadsafe(fn)` must be thread-safe and must not raise. It
  must return before `fn` runs, even when called on the host thread, and must
  never wait for `fn` to run, which can deadlock. It's called both on the
  host thread, where it must not wait for the host loop at all, and from the
  I/O thread, where it may wait for the host loop to accept `fn`, but then
  the run, and the interpreter's exit, hang if the host loop stops while it
  waits. For example:
  - WinUI: `lambda fn: dispatcher_queue.try_enqueue(fn)`;
  - Tk: see [tkinter](#tkinter), where every call from another thread waits
    for Tk's main loop;
  - Qt: use a queued connection, not a direct, automatic or blocking one.

  If it raises when `start_guest_run()` first calls it, without having run
  `fn`, the run is cleaned up (the main task, and any other task `async_fn`
  created, cancelled, and the loop closed) and the exception propagates.
- `done_callback(task)` runs on the host thread after the run has finished:
  remaining tasks cancelled, async generators and the default executor shut
  down, and the loop closed.
- Between batches, host code (a button handler, say) schedules guest work with
  `loop.call_soon_threadsafe()`, as it would from any other thread. To cancel
  the run from the host, use `loop.call_soon_threadsafe(task.cancel)`.
- That goes for everything host code asks of the guest, although it runs on
  the loop's thread: create a task with
  `loop.call_soon_threadsafe(loop.create_task, coro)`, say, and complete a
  future with `loop.call_soon_threadsafe(fut.set_result, value)`. The loop's
  other methods, such as `call_soon()` and `create_task()`, are for guest
  code.
- Signal handling stays with the host: `loop.add_signal_handler()` raises
  `RuntimeError`.

The loop is asyncio's own `SelectorEventLoop`, or on Windows its
`ProactorEventLoop`, via private subclasses. Event loop policies are not
consulted, so it can't run uvloop.

### tkinter

Every tkinter call made from another thread waits for Tk's main loop. This
recipe posts with Tk's `after idle`, through `root.tk.call()`, which waits
too:

```python
import asyncio
import collections
import tkinter as tk

from backports.asyncio.guest import start_guest_run

root = tk.Tk()
label = tk.Label(root, text='0', width=20)
label.pack()


async def main():
    for n in range(1, 1000):
        await asyncio.sleep(1)
        label['text'] = str(n)


posted = collections.deque()


def run_posted():
    # One 'after idle' per post, so each event runs exactly one function,
    # and Tk gets to paint between a busy guest's batches.
    posted.popleft()()


run_posted_name = root.register(run_posted)


def host_post(fn):
    posted.append(fn)
    root.tk.call('after', 'idle', run_posted_name)


def on_close():
    # Not root.destroy(): the run needs Tk's main loop until done_callback.
    # (If the run is abandoned after a failure, done_callback never comes,
    # so mainloop() never returns, and the process keeps running with no
    # window.)
    root.withdraw()
    task.get_loop().call_soon_threadsafe(task.cancel)


def on_done(task):
    root.destroy()


root.protocol('WM_DELETE_WINDOW', on_close)
task = start_guest_run(
    main, run_sync_soon_threadsafe=host_post, done_callback=on_done)
root.mainloop()
```

What to know about it:

- `root.tk.call()` is thread-safe with a threaded Tcl, the default. But called
  from another thread, it raises `RuntimeError` if the thread that created the
  root isn't in `mainloop()` within about a second, and that abandons the
  run. So run `root.mainloop()`, not `update()` in a loop, until
  `done_callback`.
- From another thread, `root.tk.call()` doesn't return until Tk's main loop
  has run the `after idle` command, though not `fn` itself. So the host
  function blocks the I/O thread while the main thread is busy: asyncio's I/O
  waits for Tk.
- If the root is destroyed while such a call is waiting, the I/O thread waits
  forever, and because it isn't a daemon thread, the interpreter never exits.
  On the main thread, the call raises `TclError` after the root is destroyed.
  So never destroy the root before `done_callback` runs. On close, withdraw the
  window and cancel the run, and destroy the root in `done_callback`.
- `after idle`, not `after 0`: Tk redraws widgets in idle handlers, which run
  only when no other events are pending. So a busy guest posting its next
  batch with `after 0` would starve them, while `after idle` takes turns with
  them. Loop code must not call `update()` or `update_idletasks()`: they
  could run a function the run has already posted while the batch is still
  running.

On Unix, `root.tk.createfilehandler()` avoids blocking: the host function
appends `fn` to the deque and sends a byte, without blocking, to one end of a
`socket.socketpair()`. The file handler on the other end reads the bytes and
posts the deque's runner with `after idle`, rather than running it: a busy
guest keeps the socket readable, so running the deque directly would starve
redraws, as `after 0` would. On Windows, tkinter has nothing that both
avoids blocking and is event-driven, so `after idle` from the I/O thread
stays the way to post there, with the root destroyed only in `done_callback`.

## Private parts it relies on

Driving asyncio from outside needs asyncio's private parts until Python ships
guest mode. That is why this exists as one tested package rather than a copy
in each app. The package uses:

- `BaseEventLoop._run_once()`'s internals (`_ready`, `_scheduled`, handles'
  `_run()`, and so on), to run it as separate steps;
- `_run_forever_setup()`/`_run_forever_cleanup()` on Python 3.13 and later,
  with a copy for 3.11 and 3.12;
- on Windows, `IocpProactor`'s completion cache, to wait for completions on
  one thread and process them on the other;
- `asyncio.events._get_running_loop()`/`_set_running_loop()`.

The CI workflow tests the built wheel on each supported Python version, on
Linux, Windows (x64 and ARM64) and macOS, every week as well as on each push.
Two experimental jobs also run: CPython's development branch, and the
free-threaded 3.14t build, on Linux and Windows. Free-threaded builds pass
there, but aren't claimed as supported yet.

## Running the tests

```
python -X dev -m unittest discover -s tests
```

with the package installed, or with `PYTHONPATH=src`. The tests use the
backport's own `start_guest_run()`, even on a Python that has one.

## Releasing

Only once CPython has merged guest mode:

1. Match `_guest.py` and the tests to the merged `Lib/asyncio/guest.py` and
   `Lib/test/test_asyncio/test_guest.py`: the API (names, keywords, behavior)
   must be the same, because on that Python version and later
   `backports.asyncio.guest.start_guest_run` *is* `asyncio.start_guest_run`.
2. Set `requires-python`'s and the CI matrix's lower bound to the oldest
   supported version, and the `version` in `pyproject.toml`.
3. CI must pass on every job.

## License

It is derived from CPython, and distributed under the same license, the PSF
License Agreement: see [LICENSE](LICENSE).
