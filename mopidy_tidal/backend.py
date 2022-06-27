from __future__ import unicode_literals

import logging
import os
import json

from mopidy import backend

from pykka import ThreadingActor

from tidaloauth4mopidy import Config, Session, Quality

from mopidy_tidal import context, library, playback, playlists, Extension
from mopidy_tidal.auth_http_server import start_oauth_deamon

logger = logging.getLogger(__name__)


class TidalBackend(ThreadingActor, backend.Backend):
    def __init__(self, config, audio):
        super(TidalBackend, self).__init__()
        self.session = None
        self._config = config
        self._token = config['tidal']['token']
        self._oauth = config['tidal']['oauth']
        self._oauth_port = config['tidal'].get('oauth_port')
        self.image_search = config['tidal']['image_search']
        self.quality = self._config['tidal']['quality']
        self.playback = playback.TidalPlaybackProvider(audio=audio,
                                                       backend=self)
        self.library = library.TidalLibraryProvider(backend=self)
        self.playlists = playlists.TidalPlaylistsProvider(backend=self)
        if config['tidal']['spotify_proxy']:
            self.uri_schemes = ['tidal', 'spotify']
        else:
            self.uri_schemes = ['tidal']

    def oauth_login_new_session(self, oauth_file):
        # create a new session
        self._session.login_oauth_simple(function=logger.info)
        if self._session.check_login():
            # store current OAuth session
            data = {}
            data['token_type'] = {'data': self._session.token_type}
            data['session_id'] = {'data': self._session.session_id}
            data['access_token'] = {'data': self._session.access_token}
            data['refresh_token'] = {'data': self._session.refresh_token}
            with open(oauth_file, 'w') as outfile:
                json.dump(data, outfile)

    def on_start(self):
        logger.info("Connecting to TIDAL.. Quality = %s" % self.quality)
        config = Config(self._token, self._oauth, quality=Quality(self.quality))
        self.session = Session(config)
        if self._oauth_port:
            start_oauth_deamon(self.session, self._oauth_port)

