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

- `run_sync_soon_threadsafe(fn)` must be thread-safe, must not block, and must
  not raise. For example:
  - WinUI: `lambda fn: dispatcher_queue.try_enqueue(fn)`;
  - Tk: append `fn` to a queue, then `root.call('after', 'idle', name)`;
  - Qt: post to an object on the GUI thread with a queued signal.
- `done_callback(task)` runs on the host thread after the run has finished:
  remaining tasks cancelled, async generators and the default executor shut
  down, and the loop closed.
- Between batches, host code (a button handler, say) schedules guest work with
  `loop.call_soon_threadsafe()`, as it would from any other thread. To cancel
  the run from the host, use `loop.call_soon_threadsafe(task.cancel)`.
- Signal handling stays with the host: `loop.add_signal_handler()` raises
  `RuntimeError`.

The loop is asyncio's own `SelectorEventLoop`, or on Windows its
`ProactorEventLoop`, via private subclasses. Event loop policies are not
consulted, so it can't run uvloop.

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

The CI workflow tests it on each supported Python version and on CPython's
development branch, on Windows (x64 and ARM64), Linux and macOS. macOS and
free-threaded builds (3.13t, 3.14t) haven't been tested yet: the classifiers
claim Windows and Linux only, until CI passes there.

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
3. Add the repository's Source and Issues links to `[project.urls]`.
4. CI must pass on every job, including macOS.

## License

It is derived from CPython, and distributed under the same license, the PSF
License Agreement: see [LICENSE](LICENSE).
