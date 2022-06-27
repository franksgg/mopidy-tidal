from __future__ import unicode_literals

import logging
import operator
from typing import Optional, Union, Tuple, Collection

from tidalapi.models import Playlist as TidalPlaylist

from mopidy import backend
from mopidy.models import Playlist as MopidyPlaylist, Ref

from mopidy_tidal import display
from mopidy_tidal import full_models_mappers
from mopidy_tidal.helpers import to_timestamp
from mopidy_tidal.lru_cache import LruCache


logger = logging.getLogger(__name__)


class PlaylistCache(LruCache):
    def __getitem__(
            self, key: Union[str, TidalPlaylist], *args, **kwargs
    ) -> MopidyPlaylist:
        uri = key.id if isinstance(key, TidalPlaylist) else key
        uri = (
            f'tidal:playlist:{uri}'
            if not uri.startswith('tidal:playlist:')
            else uri
        )

        playlist = super().__getitem__(uri, *args, **kwargs)
        if (
            playlist and isinstance(key, TidalPlaylist) and
            to_timestamp(key.last_updated) >
            to_timestamp(playlist.last_modified)
        ):
            # The playlist has been updated since last time:
            # we should refresh the associated cache entry
            logger.info('The playlist "%s" has been updated: refresh forced', key.name)
            raise KeyError(uri)

        return playlist


class TidalPlaylistsProvider(backend.PlaylistsProvider):

    def __init__(self, *args, **kwargs):
        super(TidalPlaylistsProvider, self).__init__(*args, **kwargs)
        self._playlists = PlaylistCache()

    def _calculate_added_and_removed_playlist_ids(self) \
            -> Tuple[Collection[str], Collection[str]]:
        session = self.backend._session
        updated_playlists = [
            *session.user.favorites.playlists(),
            *session.user.playlists(),
        ]

        updated_ids = set(pl.id for pl in updated_playlists)
        if not self._playlists:
            return updated_ids, set()

        current_ids = set(uri.split(':')[-1] for uri in self._playlists.keys())
        added_ids = updated_ids.difference(current_ids)
        removed_ids = current_ids.difference(updated_ids)
        self._playlists.prune(*[
            uri for uri in self._playlists.keys()
            if uri.split(':')[-1] in removed_ids
        ])

        return added_ids, removed_ids

    def _has_changes(self, playlist: MopidyPlaylist):
        upstream_playlist = self.backend._session.get_playlist(playlist.uri.split(':')[-1])
        if not upstream_playlist:
            return True

        upstream_last_updated_at = to_timestamp(getattr(upstream_playlist, 'last_updated', None))
        local_last_updated_at = to_timestamp(playlist.last_modified)

        if not upstream_last_updated_at:
            logger.warning(
                'You are using a version of python-tidal that does not '
                'support last_updated on playlist objects'
            )
            return True

        if upstream_last_updated_at > local_last_updated_at:
            logger.info(
                'The playlist "%s" has been updated: refresh forced', playlist.name
            )
            return True

        return False

    def as_list(self):
        added_ids, _ = self._calculate_added_and_removed_playlist_ids()
        if added_ids:
            self.refresh()

        logger.debug("Listing TIDAL playlists..")
        refs = [
            Ref.playlist(uri=pl.uri, name=pl.name)
            for pl in self._playlists.values()]
        return sorted(refs, key=operator.attrgetter('name'))

    def _get_or_refresh_playlist(self, uri) -> Optional[MopidyPlaylist]:
        if not self._playlists:
            self.refresh()

        playlist = self._playlists.get(uri)
        if playlist is None:
            return None
        if self._has_changes(playlist):
            self.refresh()
        return self._playlists.get(uri)

    def get_items(self, uri):
        playlist = self._get_or_refresh_playlist(uri)
        if not playlist:
            return []

        return [Ref.track(uri=t.uri, name=t.name) for t in playlist.tracks]

    def create(self, name):
        raise NotImplementedError

    def delete(self, uri):
        raise NotImplementedError

    def lookup(self, uri):
        return self._get_or_refresh_playlist(uri)

    def refresh(self):
        logger.debug("Refreshing TIDAL playlists..")
        playlists = {}
        session = self.backend.session

        plists = session.user.favorites.playlists()
        for pl in plists:
            pl.name = display.fav_item(pl.name)
        # Append favourites to end to keep the tagged name if there are
        # duplicates
        plists = session.user.playlists() + plists
        mapped_playlists = {}

        for pl in plists:
            uri = "tidal:playlist:" + pl.id
            # Cache hit case
            if pl in self._playlists:
                continue

            # Cache miss case
            pl_tracks = session.get_playlist_tracks(pl.id)
            tracks = full_models_mappers.create_mopidy_tracks(pl_tracks)
            pl_name = pl.name
            playlists[uri] = Playlist(uri=uri,
                                      name=display.tidal_item(pl_name),
                                      tracks=tracks,
                                      last_modified=pl.last_updated)

        self._playlists.update(mapped_playlists)
        backend.BackendListener.send('playlists_loaded')
        logger.info("TIDAL playlists refreshed")

    def save(self, playlist):
        raise NotImplementedError
