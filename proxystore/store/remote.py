"""Remote Store Abstract Class"""
from __future__ import annotations

import logging
import time

from abc import ABCMeta, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import proxystore as ps
from proxystore.factory import Factory
from proxystore.store.base import Store
from proxystore.store.cache import LRUCache

_default_pool = ThreadPoolExecutor()
logger = logging.getLogger(__name__)


class RemoteFactory(Factory):
    """Base Factory for Instances of RemoteStore

    Adds support for asynchronously retrieving objects from a
    :class:`RemoteStore <.RemoteStore>` backend and optional, strict guarentees
    on object versions.

    The factory takes the `store_type` and `store_args` parameters that are
    used to reinitialize the backend store if the factory is sent to a remote
    process backend has not already been initialized.
    """

    def __init__(
        self,
        key: str,
        store_type: Store,
        store_name: str,
        store_kwargs: Dict[str, Any] = {},
        *,
        evict: bool = False,
        serialize: bool = True,
        strict: bool = False,
    ) -> None:
        """Init RemoteFactory

        Args:
            key (str): key corresponding to object in store.
            store_type (Store): type of store this factory will resolve an
                object from.
            store_name (str): name of store
            store_kwargs (dict): optional keyword arguments used to
                reinitialize store.
            evict (bool): If True, evict the object from the store once
                :func:`resolve()` is called (default: False).
            serialize (bool): if True, object in store is serialized and
                should be deserialized upon retrival (default: True).
            strict (bool): guarentee object produce when this object is called
                is the most recent version of the object associated with the
                key in the store (default: False).
        """
        self.key = key
        self.store_type = store_type
        self.store_name = store_name
        self.store_kwargs = store_kwargs
        self.evict = evict
        self.serialize = serialize
        self.strict = strict
        self._obj_future = None

    def __getnewargs_ex__(self):
        """Helper method for pickling"""
        return (
            self.key,
            self.store_type,
            self.store_name,
            self.store_kwargs,
        ), {
            'evict': self.evict,
            'serialize': self.serialize,
            'strict': self.strict,
        }

    def _get_store(self) -> Store:
        """Helper method for reinitializing the store"""
        store = ps.store.get_store(self.store_name)
        if store is None:
            store = ps.store.init_store(
                self.store_type, self.store_name, **self.store_kwargs
            )
        return store

    def resolve(self) -> Any:
        """Get object associated with key from store"""
        if self._obj_future is not None:
            obj = self._obj_future.result()
            self._obj_future = None
            return obj

        store = self._get_store()

        obj = store.get(
            self.key, deserialize=self.serialize, strict=self.strict
        )
        if self.evict:
            store.evict(self.key)
        return obj

    def resolve_async(self) -> None:
        """Asynchronously get object associated with key from store"""
        store = self._get_store()

        # If the value is locally cached by the value server, starting up
        # a separate thread to retrieve a cached value will be slower than
        # just getting the value from the cache
        if store.is_cached(self.key, strict=self.strict):
            return

        self._obj_future = _default_pool.submit(
            store.get,
            self.key,
            deserialize=self.serialize,
            strict=self.strict,
        )


class RemoteStore(Store, metaclass=ABCMeta):
    """Abstraction for interacting with a remote key-value store

    Provides base functionality for interaction with a remote store including
    serialization and caching.
    Subclasses of :class:`RemoteStore` must implement
    :func:`evict() <Store.evict()>`, :func:`exists() <Store.exists()>`,
    :func:`get_str()`, :func:`set_str()` and :func:`proxy() <Store.proxy()>`.
    The :class:`RemoteStore` handles the caching.

    :class:`RemoteStore` stores key-string pairs, i.e., objects passed to
    :func:`get()` or :func:`set()` will be appropriately (de)serialized.
    Functionality for serialized, caching, and strict guarentees are already
    provided in :func:`get()` and :func:`set()`.
    """

    def __init__(self, name: str, *, cache_size: int = 0) -> None:
        """Init RemoteStore

        Args:
            name (str): name of the store instance.
            cache_size (int): size of local cache (in # of objects). If 0,
                the cache is disabled (default: 0).

        Raises:
            ValueError:
                if `cache_size` is negative.
        """
        if cache_size < 0:
            raise ValueError('Cache size cannot be negative')
        self.cache_size = cache_size
        self._cache = LRUCache(cache_size) if cache_size > 0 else None
        super(RemoteStore, self).__init__(name)

    @abstractmethod
    def get_bytes(self, key: str) -> Optional[bytes]:
        """Get serialized object from remote store

        Args:
            key (str): key corresponding to object.

        Returns:
            serialized object or `None` if it does not exist.
        """
        raise NotImplementedError

    @abstractmethod
    def set_bytes(self, key: str, data: bytes) -> None:
        """Set serialized object in remote store with key

        Args:
            key (str): key corresponding to object.
            data (bytes): serialized object.
        """
        raise NotImplementedError

    def get(
        self,
        key: str,
        *,
        deserialize: bool = True,
        strict: bool = False,
        default: Optional[object] = None,
    ) -> Optional[object]:
        """Return object associated with key

        Args:
            key (str): key corresponding to object.
            deserialize (bool): deserialize object if True. If objects
                are custom serialized, set this as False (default: True).
            strict (bool): guarentee returned object is the most recent
                version (default: False).
            default: optionally provide value to be returned if an object
                associated with the key does not exist (default: None).

        Returns:
            object associated with key or `default` if key does not exist.
        """
        if self.is_cached(key, strict=strict):
            value = self._cache.get(key)[1]
            logger.debug(
                f"GET key='{key}' FROM {self.__class__.__name__}"
                f"(name='{self.name}'): was_cached=True"
            )
            return value

        value = self.get_bytes(key)
        if value is not None:
            timestamp = float(self.get_bytes(key + '_timestamp').decode())
            if deserialize:
                value = ps.serialize.deserialize(value)
            if self._cache is not None:
                self._cache.set(key, (timestamp, value))
            logger.debug(
                f"GET key='{key}' FROM {self.__class__.__name__}"
                f"(name='{self.name}'): was_cached=False"
            )
            return value

        logger.debug(
            f"GET key='{key}' FROM {self.__class__.__name__}"
            f"(name='{self.name}'): key did not exist, returned default"
        )
        return default

    def is_cached(self, key: str, *, strict: bool = False) -> bool:
        """Check if object is cached locally

        Args:
            key (str): key corresponding to object.
            strict (bool): guarentee object in cache is most recent version
                (default: False).

        Returns:
            bool
        """
        if self._cache is None:
            return False

        if self._cache.exists(key):
            if strict:
                store_timestamp = float(
                    self.get_bytes(key + '_timestamp').decode()
                )
                cache_timestamp = self._cache.get(key)[0]
                return cache_timestamp >= store_timestamp
            return True

        return False

    def set(
        self, obj: Any, *, key: Optional[str] = None, serialize: bool = True
    ) -> str:
        """Set key-object pair in store

        Args:
            obj (object): object to be placed in the store.
            key (str, optional): key to use with the object. If the key is not
                provided, one will be created.
            serialize (bool): serialize object if True. If object is already
                custom serialized, set this as False (default: True).

        Returns:
            key (str)
        """
        if serialize:
            obj = ps.serialize.serialize(obj)
        if key is None:
            key = self.create_key(obj)

        self.set_bytes(key, obj)
        self.set_bytes(key + '_timestamp', str(time.time()).encode())
        logger.debug(
            f"SET key='{key}' IN {self.__class__.__name__}"
            f"(name='{self.name}')"
        )
        return key
