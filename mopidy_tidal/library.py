from __future__ import unicode_literals

import logging
from typing import List

from requests.exceptions import HTTPError

from mopidy import backend, models

from mopidy.models import Image, SearchResult

from mopidy_tidal import (
    full_models_mappers,
    ref_models_mappers,
)

from mopidy_tidal.lru_cache import with_cache, image_cache

from mopidy_tidal.playlists import PlaylistCache

from mopidy_tidal.utils import apply_watermark

from mopidy_tidal.spotify_proxy import SpotifyProxy

logger = logging.getLogger(__name__)


class TidalLibraryProvider(backend.LibraryProvider):
    root_directory = models.Ref.directory(uri='tidal:directory', name='Tidal')

    def get_distinct(self, field, query=None):
        from mopidy_tidal.search import tidal_search

        logger.debug("Browsing distinct %s with query %r", field, query)
        session = self.backend.session

        if not query:  # library root
            if field == "artist" or field == "albumartist":
                return [apply_watermark(a.name) for a in
                        session.user.favorites.artists()]
            elif field == "album":
                return [apply_watermark(a.name) for a in
                        session.user.favorites.albums()]
            elif field == "track":
                return [apply_watermark(t.name) for t in
                        session.user.favorites.tracks()]
        else:
            if field == "artist":
                return [apply_watermark(a.name) for a in
                        session.user.favorites.artists()]
            elif field == "album" or field == "albumartist":
                artists, _, _ = tidal_search(session,
                                             query=query,
                                             exact=True)
                if len(artists) > 0:
                    artist = artists[0]
                    artist_id = artist.uri.split(":")[2]
                    return [apply_watermark(a.name) for a in
                            session.get_artist_albums(artist_id)]
            elif field == "track":
                return [apply_watermark(t.name) for t in
                        session.user.favorites.tracks()]

        return []

    def browse(self, uri):
        logger.debug("Browsing uri %s", uri)
        if not uri or not uri.startswith("tidal:"):
            return []

        session = self.backend.session

        # summaries
        if uri == self.root_directory.uri:
            return ref_models_mappers.create_root()

        elif uri == "tidal:my_artists":
            return ref_models_mappers.create_artists(
                    session.user.favorites.artists())
        elif uri == "tidal:my_albums":
            return ref_models_mappers.create_albums(
                    session.user.favorites.albums())
        elif uri == "tidal:my_playlists":
            return ref_models_mappers.create_playlists(
                    session.user.favorites.playlists())
        elif uri == "tidal:my_tracks":
            return ref_models_mappers.create_tracks(
                    session.user.favorites.tracks())
        elif uri == "tidal:moods":
            return ref_models_mappers.create_moods(
                    session.get_moods())
        elif uri == "tidal:genres":
            return ref_models_mappers.create_genres(
                    session.get_genres())

        # details

        parts = uri.split(':')
        nr_of_parts = len(parts)

        if nr_of_parts == 4 and parts[1] == "album":
            return ref_models_mappers.create_tracks(
                    session.get_album_tracks(parts[3]))

        if nr_of_parts == 3 and parts[1] == "artist":
            top_10_tracks = session.get_artist_top_tracks(parts[2])[:10]
            albums = ref_models_mappers.create_albums(
                    session.get_artist_albums(parts[2]))
            return albums + ref_models_mappers.create_tracks(top_10_tracks)

        if nr_of_parts == 3 and parts[1] == "playlist":
            return ref_models_mappers.create_tracks(
                session.get_playlist_tracks(parts[2]))

        if nr_of_parts == 3 and parts[1] == "mood":
            return ref_models_mappers.create_playlists(
                session.get_mood_playlists(parts[2]))

        if nr_of_parts == 3 and parts[1] == "genre":
            return ref_models_mappers.create_playlists(
                session.get_genre_items(parts[2], 'playlists'))

        logger.error('Unknown uri for browse request: %s', uri)
        return []

    def search(self, query=None, uris=None, exact=False):
        from mopidy_tidal.search import tidal_search

        try:
            artists, albums, tracks = \
                tidal_search(self.backend.session,
                             query=query,
                             exact=exact)
            return SearchResult(artists=artists,
                                albums=albums,
                                tracks=tracks)
        except Exception as ex:
            logger.critical("%r", ex)

    @staticmethod
    def _get_image_uri(obj):
        try:
            return obj.picture(width=750, height=750)
        except AttributeError:
            pass

    def _get_images(self, uri) -> List[Image]:
        assert uri.startswith('tidal:'), f'Invalid TIDAL URI: {uri}'

        parts = uri.split(':')
        item_type = parts[1]
        if item_type == 'track':
            # For tracks, retrieve the artwork of the associated album
            item_type = 'album'
            item_id = parts[3]
            uri = ':'.join([parts[0], 'album', parts[3]])
        else:
            item_id = parts[2]

        if uri in self._image_cache:
            # Cache hit
            return self._image_cache[uri]

        logger.debug('Retrieving %r from the API', uri)
        getter_name = f'get_{item_type}'
        getter = getattr(self._session, getter_name, None)
        assert getter, f'No such session method: {getter_name}'

        item = getter(item_id)
        if not item:
            logger.debug('%r is not available on the backend', uri)
            return []

        img_uri = self._get_image_uri(item)
        if not img_uri:
            logger.debug('%r has no associated images', uri)
            return []

        logger.debug('Image URL for %r: %r', uri, img_uri)
        return [Image(uri=img_uri, width=320, height=320)]

    def get_images(self, uris):
        logger.debug("Searching Tidal for images for %r" % uris)
        return {uri: self._get_images(uri) for uri in uris}

    def _get_images(self, uri):
        parts = uri.split(':')
        if parts[1] == 'track':
            uri = '{0}:album:{2}:{3}'.format(*parts)
            parts = uri.split(':')
        uri_image = image_cache.hit(uri)
        if uri_image is None and self.backend.image_search:
            logger.info('CACHE HIT MISS: %s', uri)
            if parts[1] == 'artist':
                uri_image = self.backend.session.get_artist(artist_id=parts[2]).image
            elif parts[1] == 'album':
                uri_image = self.backend.session.get_album(album_id=parts[3]).image
            logger.info("Setting image cache (%s) with %s = %s", len(image_cache), uri, uri_image)
            image_cache[uri] = uri_image
        return [Image(uri=uri_image, width=512, height=512)] if uri_image else ()

    def lookup(self, uris=None):
        logger.debug("Lookup uris %r", uris)
        if isinstance(uris, str):
            uris = [uris]
        if not hasattr(uris, '__iter__'):
            uris = [uris]
        return [t for tracks in (self._lookup(uri) for uri in uris) for t in tracks]

    @with_cache
    def _lookup(self, uri):
        parts = uri.split(':')
        if uri.startswith('tidal:track:'):
            return self._lookup_track(track_id=parts[4])
        elif uri.startswith('tidal:album:'):
            return self._lookup_album(album_id=parts[3])
        elif uri.startswith('tidal:artist:'):
            return self._lookup_artist(artist_id=parts[2])

    def _lookup_track(self, track_id):
        track = self.backend.session.get_track(track_id)
        return [full_models_mappers.create_mopidy_track(track)]

    def _lookup_album(self, album_id):
        logger.info("Looking up album ID: %s", album_id)
        tracks = self.backend.session.get_album_tracks(album_id)
        return full_models_mappers.create_mopidy_tracks(tracks)

    def _lookup_artist(self, artist_id):
        logger.info("Looking up artist ID: %s", artist_id)
        tracks = self.backend.session.get_artist_top_tracks(artist_id)
        return full_models_mappers.create_mopidy_tracks(tracks)
