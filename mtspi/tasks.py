"""Background tasks that nobody awaits.

The event loop keeps only weak references to its tasks, and a stream's protocol keeps only a weak reference to its
reader. A task that just waits on a stream can therefore be garbage collected in mid-flight ("Task was destroyed
but it is pending!"). It happened on the bike on 16 Nov 2026: the task serving the dashboard's control channel
vanished after a quarter of an hour, and when the dashboard reopened the channel after a radio dropout nobody
answered until the bridge was restarted. spawn() keeps every such task alive until it ends.
"""
import asyncio

_alive = set()


def spawn(coro):
    task = asyncio.ensure_future(coro)
    _alive.add(task)
    task.add_done_callback(_alive.discard)
    return task
