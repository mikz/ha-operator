"""Cancellation-safe completion of owned I/O before teardown or acknowledgement."""

import asyncio


async def async_settle[T](future: asyncio.Future[T]) -> tuple[T, bool]:
    """Wait out repeated cancellation, then consume exactly one I/O result.

    Python 3.14 logs exceptions from cancelled shields, even if a later shield
    consumes the same future. Shield a completion signal that cannot fail, and
    retrieve the actual result after it settles instead.
    """
    completion: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def completed(_future: asyncio.Future[T]) -> None:
        completion.set_result(None)

    future.add_done_callback(completed)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            cancelled = True
    return future.result(), cancelled
