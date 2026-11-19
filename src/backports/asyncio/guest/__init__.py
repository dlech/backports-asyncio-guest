"""
Backport of asyncio.start_guest_run(): run asyncio as a guest inside another
event loop, such as a GUI toolkit's, that keeps control of its thread.

    from backports.asyncio.guest import start_guest_run

    task = start_guest_run(
        main,
        run_sync_soon_threadsafe=host_queue.put,  # runs fn on the host thread
        done_callback=on_done,
    )

On a Python whose asyncio has start_guest_run(), this is that function: this
package is only released once CPython has merged guest mode, with the same
API, so the two are interchangeable.
"""

import asyncio

if hasattr(asyncio, 'start_guest_run'):
    from asyncio import start_guest_run
else:
    from ._guest import start_guest_run

__all__ = ('start_guest_run',)
