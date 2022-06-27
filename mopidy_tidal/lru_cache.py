from __future__ import unicode_literals

import logging
import os
import pathlib
import pickle

from collections import OrderedDict
from functools import wraps


logger = logging.getLogger(__name__)


class LruCache(OrderedDict):
    def __init__(self, max_size=1024, default_value=''):
        if max_size <= 0:
            raise ValueError('Invalid size')
        OrderedDict.__init__(self)
        self._max_size = max_size
        self._check_limit()
        self._default_value = default_value

    @property
    def max_size(self):
        return self._max_size

    @property
    def persist(self):
        return self._persist

    def _cache_filename(self, key: str) -> str:
        parts = key.split(':')
        assert len(parts) > 2, f'Invalid TIDAL ID: {key}'
        cache_dir = os.path.join(self._cache_dir, parts[1], parts[2][:2])
        pathlib.Path(cache_dir).mkdir(parents=True, exist_ok=True)
        return os.path.join(cache_dir, f'{key}.cache')

    def _get_from_storage(self, key):
        cache_file = self._cache_filename(key)
        err = KeyError(key)
        if not os.path.isfile(cache_file):
            # Cache miss on the filesystem
            raise err

        # Cache hit on the filesystem
        with open(cache_file, 'rb') as f:
            try:
                value = pickle.load(f)
            except Exception as e:
                # If the cache entry on the filesystem is corrupt, reset it
                logger.warning('Could not deserialize cache file %s: '
                    'refreshing the entry: %s', cache_file, e)
                self._reset_stored_entry(key)
                raise err

        # Store the filesystem item in memory
        if value is not None:
            self.__setitem__(key, value, _sync_to_fs=False)
        logger.debug(f'Filesystem cache hit for {key}')
        return value

    def __getitem__(self, key, *_, **__):
        try:
            # Cache hit in memory
            return super().__getitem__(key)
        except KeyError as e:
            if not self.persist:
                # No persisted storage -> cache miss
                raise e

        # Check on the persisted cache
        return self._get_from_storage(key)

    def __setitem__(self, key, value, _sync_to_fs=True, *_, **__):
        if super().__contains__(key):
            del self[key]

        OrderedDict.__setitem__(self, key, self._default_value if value is None else value)
        self._check_limit()

    def _check_limit(self):
        if self.max_size:
            # delete oldest entries
            while len(self) > self.max_size:
                self.popitem(last=False)


class SearchCache(LruCache):
    def __init__(self, func):
        super().__init__(persist=False)
        self._func = func

    def __call__(self, *args, **kwargs):
        key = str(SearchKey(**kwargs))
        cached_result = self.get(key)
        logger.info("Search cache miss" if cached_result is None
                    else "Search cache hit")
        if cached_result is None:
            cached_result = self._func(*args, **kwargs)
            self[key] = cached_result

        return cached_result


class SearchKey(object):
    def __init__(self, **kwargs):
        fixed_query = self.fix_query(kwargs["query"])
        self._query = tuple(sorted(fixed_query.items()))
        self._exact = kwargs["exact"]
        self._hash = None

    def __hash__(self):
        if self._hash is None:
            self._hash = hash(self._exact)
            self._hash ^= hash(repr(self._query))

        return self._hash

    def __str__(self):
        return f'tidal:search:{self.__hash__()}'

    def __eq__(self, other):
        if not isinstance(other, SearchKey):
            return False

        return self._exact == other._exact and \
            self._query == other._query

    @staticmethod
    def fix_query(query):
        """
        Removes some query parameters that otherwise will lead to a cache miss.
        Eg: 'track_no' since we can't query TIDAL for a specific album's track.
        :param query: query dictionary
        :return: sanitized query dictionary
        """
        query.pop("track_no", None)
        return query


track_cache = LruCache(max_size=1024*16)
image_cache = LruCache(max_size=1024*16)


def cache_track(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        item = func(*args, **kwargs)
        track_cache[item.uri] = item
        return item
    return wrapper


def cache_image(func):
    @wraps(func)
    def wrapper(tidal_item, *args, **kwargs):
        item = func(tidal_item, *args, **kwargs)
        image_cache[item.uri] = tidal_item.image
        return item
    return wrapper


def with_cache(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        uri = args[-1]
        track = track_cache.hit(uri)
        if track is not None:
            logger.debug("Found cached: %s", uri)
            return [track]
        return func(*args, **kwargs)
    return wrapper
