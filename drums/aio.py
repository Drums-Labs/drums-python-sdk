"""asyncio variants: the same calls, run in worker threads (the transport is blocking urllib).

    import asyncio, drums.aio
    async def main():
        c = await drums.aio.connect(url, token)
        envs = await asyncio.gather(*[c.create(task="035f41ba") for _ in range(16)], return_exceptions=True)
        rewards = await asyncio.gather(*[e.verify(strict=False) for e in envs if isinstance(e, drums.aio.AsyncEnv)])
"""
from __future__ import annotations

import asyncio

from . import DEFAULT_IMAGE, Client, Env, connect as _connect


async def connect(url=None, token=None, **kw) -> "AsyncClient":
    return AsyncClient(await asyncio.to_thread(_connect, url, token, **kw))


class AsyncClient:
    def __init__(self, client: Client):
        self.sync = client

    async def create(self, image: str = DEFAULT_IMAGE, task=None, **kw) -> "AsyncEnv":
        return AsyncEnv(await asyncio.to_thread(self.sync.create, image, task, **kw))

    async def health(self):
        return await asyncio.to_thread(self.sync.health)

    async def metrics(self):
        return await asyncio.to_thread(self.sync.metrics)

    async def task(self, task_id):
        return await asyncio.to_thread(self.sync.task, task_id)


class AsyncEnv:
    def __init__(self, env: Env):
        self.sync = env

    id = property(lambda self: self.sync.id)
    episode = property(lambda self: self.sync.episode)

    async def step(self, action, screenshot: bool = False):
        return await asyncio.to_thread(self.sync.step, action, screenshot)

    async def observe(self):
        return await asyncio.to_thread(self.sync.observe)

    async def reset(self):
        await asyncio.to_thread(self.sync.reset)
        return self

    async def verify(self, strict: bool = True):
        return await asyncio.to_thread(self.sync.verify, strict)

    async def fork(self, n: int, strict: bool = True, quiesce_timeout=None):
        return [AsyncEnv(e) for e in await asyncio.to_thread(self.sync.fork, n, strict, quiesce_timeout)]

    async def save(self, name=None, quiesce_timeout=None):
        return await asyncio.to_thread(self.sync.save, name, quiesce_timeout)

    async def wait_quiescent(self, timeout=None, window=None):
        return await asyncio.to_thread(self.sync.wait_quiescent, timeout, window)

    async def metrics(self):
        return await asyncio.to_thread(self.sync.metrics)

    async def close(self):
        return await asyncio.to_thread(self.sync.close)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def __repr__(self):
        return f"<drums.aio.AsyncEnv {self.sync!r}>"
