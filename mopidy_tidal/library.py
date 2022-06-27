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

from mopidy_tidal.lru_cache import LruCache

from mopidy_tidal.playlists import PlaylistCache

from mopidy_tidal.utils import apply_watermark

from mopidy_tidal.spotify_proxy import SpotifyProxy

logger = logging.getLogger(__name__)


class TidalLibraryProvider(backend.LibraryProvider):
    root_directory = models.Ref.directory(uri='tidal:directory', name='Tidal')

    def __init__(self, *args, **kwargs):
        super(TidalLibraryProvider, self).__init__(*args, **kwargs)
        self._artist_cache = LruCache()
        self._album_cache = LruCache()
        self._track_cache = LruCache()
        self._playlist_cache = PlaylistCache()
        self._image_cache = LruCache(directory='image')
        self.config = kwargs["backend"]._config
        if self.config["tidal"]["spotify_proxy"]:
            self.spotify_proxy = SpotifyProxy(str(self.config["tidal"]["spotify_client_id"]), 
                                              str(self.config["tidal"]["spotify_client_secret"]))

    @property
    def _session(self):
        return self.backend._session   # type: ignore

    def get_distinct(self, field, query=None):
        from mopidy_tidal.search import tidal_search

        logger.debug("Browsing distinct %s with query %r", field, query)
        session = self._session

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
            pass

        return []

    def browse(self, uri):
        logger.info("Browsing uri %s", uri)
        if not uri or not uri.startswith("tidal:"):
            return []

        session = self._session

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

        if nr_of_parts == 3 and parts[1] == "album":
            return ref_models_mappers.create_tracks(
                    session.get_album_tracks(parts[2]))

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

        logger.debug('Unknown uri for browse request: %s', uri)
        return []

    def search(self, query=None, uris=None, exact=False):
        from mopidy_tidal.search import tidal_search

        try:
            artists, albums, tracks = \
                tidal_search(self._session,
                             query=query,
                             exact=exact)
            return SearchResult(artists=artists,
                                albums=albums,
                                tracks=tracks)
        except Exception as ex:
            logger.info("EX")
            logger.info("%r", ex)

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
        logger.info("Searching Tidal for images for %r" % uris)
        images = {}

        for uri in uris:
            try:
                images[uri] = self._get_images(uri)
            except (AssertionError, AttributeError, HTTPError) as err:
                logger.error(
                    "%s when processing URI %r: %s",
                    type(err), uri, err)

        self._image_cache.update(images)
        return images

    def lookup(self, uris=None):
        logger.info("Lookup uris %r", uris)
        if isinstance(uris, str):
            uris = [uris]
        if not hasattr(uris, '__iter__'):
            uris = [uris]

        tracks = []
        cache_updates = {}
        for uri in uris:
            parts = uri.split(':')
            logger.info('URI: %s', uri)
            if uri.startswith('spotify:track:'):
                info = self.spotify_proxy.get_song_info(uri)
                if info is not None:
                    result = self.search(query={"track_name": [info["title"] + " " + " ".join(info["artists"])]})
                    if len(result.tracks) > 0:
                        tracks.append(result.tracks[0])
            if uri.startswith('tidal:track:'):
                if uri in self.track_cache:
                    tracks.append(self.track_cache[uri])
                else:
                    tracks += self._lookup_track(session, parts)
            elif uri.startswith('tidal:album'):
                tracks += self._lookup_album(session, parts)
            elif uri.startswith('tidal:artist'):
                tracks += self._lookup_artist(session, parts)
            elif uri.startswith('tidal:playlist'):
                tracks += self._lookup_playlist(session, parts)

        for uri in (uris or []):
            data = []
            try:
                parts = uri.split(':')
                item_type = parts[1]
                cache_name = f'_{parts[1]}_cache'
                cache_miss = True

                try:
                    data = getattr(self, cache_name)[uri]
                    cache_miss = not bool(data)
                except (AttributeError, KeyError):
                    pass

                if cache_miss:
                    try:
                        lookup = getattr(self, f'_lookup_{parts[1]}')
                    except AttributeError:
                        continue

                    data = cache_data = lookup(self._session, parts)
                    cache_updates[cache_name] = cache_updates.get(cache_name, {})
                    if item_type == 'playlist':
                        # Playlists should be persisted on the cache as objects,
                        # not as lists of tracks. Therefore, _lookup_playlist
                        # returns a tuple that we need to unpack
                        data, cache_data = data

                    cache_updates[cache_name][uri] = cache_data

                if item_type == 'playlist' and not cache_miss:
                    tracks += data.tracks
                else:
                    tracks += data if hasattr(data, '__iter__') else [data]
            except HTTPError as err:
                logger.error("%s when processing URI %r: %s", type(err), uri, err)

        for cache_name, new_data in cache_updates.items():
            getattr(self, cache_name).update(new_data)

        self._track_cache.update({track.uri:track for track in tracks})
        logger.info("Returning %d tracks", len(tracks))
        return tracks

    def _lookup_playlist(self, session, parts):
        playlist_uri = ':'.join(parts)
        playlist_id = parts[2]
        playlist = self._playlist_cache.get(playlist_uri)
        if playlist:
            return playlist.tracks

        tidal_playlist = session.get_playlist(playlist_id)
        tidal_tracks = session.get_playlist_tracks(playlist_id)
        pl_tracks = full_models_mappers.create_mopidy_tracks(tidal_tracks)
        pl = full_models_mappers.create_mopidy_playlist(tidal_playlist, pl_tracks)
        # We need both the list of tracks and the mapped playlist object for
        # caching purposes
        return pl_tracks, pl

    def _lookup_track(self, session, parts):
        album_id = parts[3]
        album_uri = ':'.join(['tidal', 'album', album_id])

        tracks = self._album_cache.get(album_uri)
        if tracks is None:
            tracks = session.get_album_tracks(album_id)

        track = [t for t in tracks if t.id == int(parts[4])][0]
        artist = full_models_mappers.create_mopidy_artist(track.artist)
        album = full_models_mappers.create_mopidy_album(track.album, artist)
        return [full_models_mappers.create_mopidy_track(artist, album, track)]

    def _lookup_album(self, session, parts):
        album_id = parts[2]
        album_uri = ':'.join(parts)

        tracks = self._album_cache.get(album_uri)
        if tracks is None:
            tracks = session.get_album_tracks(album_id)

        return full_models_mappers.create_mopidy_tracks(tracks)

    def _lookup_artist(self, session, parts):
        artist_id = parts[2]
        artist_uri = ':'.join(parts)

        tracks = self._artist_cache.get(artist_uri)
        if tracks is None:
            tracks = session.get_artist_top_tracks(artist_id)

        return full_models_mappers.create_mopidy_tracks(tracks)
