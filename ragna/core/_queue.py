import itertools
import platform
import re
from typing import Optional, Type, TypeVar, Union
from urllib.parse import urlsplit

import huey.api
import huey.constants
import huey.contrib.asyncio
import huey.utils
import redis

from ._components import Component
from ._config import Config
from ._utils import RagnaException


def task_config(retries: int = 0, retry_delay: int = 0):
    def decorator(fn):
        fn.__ragna_task_config__ = dict(retries=retries, retry_delay=retry_delay)
        return fn

    return decorator


_COMPONENTS: dict[Type[Component], Component] = {}


def execute(component, fn, args, kwargs):
    self = _COMPONENTS[component]
    assert self is not None
    return fn(self, *args, **kwargs)


class _Task(huey.api.Task):
    def execute(self):
        return execute(*self.args)


T = TypeVar("T", bound=Component)


class Queue:
    def __init__(self, config: Config, *, load_components: Optional[bool]):
        self._config = config
        self._huey = self._load_huey(config.rag.queue_url)

        for component in itertools.chain(
            config.rag.source_storages,
            config.rag.assistants,
        ):
            self.parse_component(component, load=load_components)

    def _load_huey(self, url: Optional[str]):
        # FIXME: we need to store_none=True here. SourceStorage.store returns None and
        #  if we wouldn't store it, waiting for a result is timing out. Maybe there is a
        #  better way to do this?
        common_kwargs = dict(name="ragna", store_none=True)
        if url == "memory":
            _huey = huey.MemoryHuey(immediate=True, **common_kwargs)
        elif platform.system() == "Windows" and re.match(r"\w:\\", url):
            # This special cases absolute paths on Windows, e.g. C:\Users\...,
            # since they don't play well with urlsplit below
            _huey = huey.FileHuey(path=url, use_thread_lock=True, **common_kwargs)
        else:
            components = urlsplit(url)
            if components.scheme in {"", "file"}:
                _huey = huey.FileHuey(
                    path=components.path, use_thread_lock=True, **common_kwargs
                )
            elif components.scheme in {"redis", "rediss"}:
                _huey = huey.RedisHuey(url=url, **common_kwargs)
                try:
                    _huey.storage.conn.ping()
                except redis.exceptions.ConnectionError:
                    raise RagnaException("Unable to connect to redis", url=url)
            else:
                raise RagnaException("Unknown URL scheme", url=url)
        # This is registering the execute function above to be called if a task is
        # enqueued. We need to create the TaskWrapper object here, because this is the
        # only way to dynamically register tasks while staying in the public API. This
        # could be replaced by
        # self._huey._registry._registry[f"{__name__}.{_Task.__name__}"] = _Task
        huey.api.TaskWrapper(_huey, execute, name=_Task.__name__)

        return _huey

    def parse_component(
        self, component: Union[Type[T], T, str], *, load: Optional[bool] = None
    ) -> Type[T]:
        if load is None:
            load = isinstance(self._huey, huey.MemoryHuey)

        if isinstance(component, type) and issubclass(component, Component):
            cls = component
            instance = None
        elif isinstance(component, Component):
            cls = type(component)
            instance = component
        elif isinstance(component, str):
            try:
                cls = next(
                    cls for cls in _COMPONENTS if cls.display_name() == component
                )
            except StopIteration:
                raise RagnaException("Unknown component", component=component)
            instance = None

        if instance is None:
            instance = _COMPONENTS.get(cls)

        if instance is not None:
            return cls

        if load:
            if not cls.is_available():
                raise RagnaException("Component not available", name=cls.display_name())

            instance = cls(self._config)

        _COMPONENTS[cls] = instance

        return cls

    async def enqueue(self, component, action, args, kwargs):
        fn = getattr(component, action)
        task = _Task(
            args=(component, fn, args, kwargs),
            **getattr(fn, "__ragna_task_config__", dict()),
        )
        result = self._huey.enqueue(task)
        output = await huey.contrib.asyncio.aget_result(result)
        if isinstance(output, huey.utils.Error):
            raise RagnaException("Task failed", **output.metadata)
        return output

    def create_worker(self, num_workers: int = 1):
        return self._huey.create_consumer(
            workers=num_workers, worker_type=huey.constants.WORKER_THREAD
        )
