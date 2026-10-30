"""asyncio variants: the same calls, run in worker threads (the transport is blocking urllib).

Each AsyncClient has its own thread pool (`max_threads`, default 1024; threads start only when needed), so N envs
driven with asyncio.gather really run N calls at once. asyncio.to_thread would use the loop's default executor,
min(32, cpu_count + 4) threads, and silently run a 256-env driver 32 calls at a time.

    import asyncio, drums.aio
    async def main():
        c = await drums.aio.connect(url, token)
        envs = await asyncio.gather(*[c.create(task="035f41ba") for _ in range(16)], return_exceptions=True)
        rewards = await asyncio.gather(*[e.verify(strict=False) for e in envs if isinstance(e, drums.aio.AsyncEnv)])
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import functools

from . import DEFAULT_IMAGE, Client, Env, ForkResult, connect as _connect

DEFAULT_MAX_THREADS = 1024


async def connect(url=None, token=None, *, max_threads: int = DEFAULT_MAX_THREADS, **kw) -> "AsyncClient":
    pool = cf.ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="drums-aio")
    c = await asyncio.get_running_loop().run_in_executor(pool, functools.partial(_connect, url, token, **kw))
    return AsyncClient(c, pool=pool)


class AsyncClient:
    def __init__(self, client: Client, pool: cf.Executor | None = None, max_threads: int = DEFAULT_MAX_THREADS):
        self.sync = client
        self.pool = pool or cf.ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="drums-aio")

    async def _run(self, fn, *a, **kw):
        return await asyncio.get_running_loop().run_in_executor(self.pool, functools.partial(fn, *a, **kw))

    async def create(self, image: str = DEFAULT_IMAGE, task=None, **kw) -> "AsyncEnv":
        return AsyncEnv(await self._run(self.sync.create, image, task, **kw), self)

    async def health(self):
        return await self._run(self.sync.health)

    async def metrics(self):
        return await self._run(self.sync.metrics)

    async def task(self, task_id):
        return await self._run(self.sync.task, task_id)

    async def delete_state(self, state):
        return await self._run(self.sync.delete_state, state)

    async def close(self):
        """Close every env this client created and has not closed (see Client.close)."""
        return await self._run(self.sync.close)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


class AsyncEnv:
    def __init__(self, env: Env, client: AsyncClient | None = None):
        self.sync = env
        self._client = client or AsyncClient(env.client)

    async def _run(self, fn, *a, **kw):
        return await self._client._run(fn, *a, **kw)

    id = property(lambda self: self.sync.id)
    episode = property(lambda self: self.sync.episode)

    async def step(self, action, screenshot: bool = False):
        return await self._run(self.sync.step, action, screenshot)

    async def observe(self):
        return await self._run(self.sync.observe)

    async def reset(self):
        await self._run(self.sync.reset)
        return self

    async def verify(self, strict: bool = True):
        return await self._run(self.sync.verify, strict)

    async def fork(self, n: int, strict: bool = True, quiesce_timeout=None, **kw):
        """As Env.fork (keep_state=, idle_timeout=, ttl=, state_idle_timeout=, state_ttl=): a ForkResult of AsyncEnvs."""
        r = await self._run(self.sync.fork, n, strict, quiesce_timeout, **kw)
        return ForkResult([AsyncEnv(e, self._client) for e in r], r.state, r.checkpoint, r.failed, r.seconds, r._client)

    async def save(self, name=None, quiesce_timeout=None, **kw):
        return await self._run(self.sync.save, name, quiesce_timeout, **kw)

    async def wait_quiescent(self, timeout=None, window=None):
        return await self._run(self.sync.wait_quiescent, timeout, window)

    async def metrics(self):
        return await self._run(self.sync.metrics)

    async def close(self):
        return await self._run(self.sync.close)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def __repr__(self):
        return f"<drums.aio.AsyncEnv {self.sync!r}>"
