#!/usr/bin/env python3
#
# server.py — Spotily Pure yt-dlp Music Streaming Server
#

import http.server
import urllib.parse
import json
import subprocess
import sys
import os
import time
import re
import threading
import base64
import tempfile
import shutil

PORT = int(os.environ.get('PORT', '8765'))

# Extra yt-dlp flags for hosted environments (e.g. datacenter IPs often get
# YouTube bot-checks). Set YTDLP_EXTRA in the host, e.g.:
#   YTDLP_EXTRA="--extractor-args youtube:player_client=android"
YTDLP_EXTRA = []
_env_extra = os.environ.get('YTDLP_EXTRA', '').strip()
if _env_extra:
    YTDLP_EXTRA = [a for a in _env_extra.split() if a]

# Hosted deployments may mount a YouTube cookies file (Render mounts secret
# files read-only at /etc/secrets/<name>). yt-dlp likes to rewrite cookie
# files, so copy the secret to a writable temp path once at startup and add
# `--cookies` automatically. Set YTDLP_COOKIE_SECRET=/etc/secrets/<name>.
_COOKIE_SRC = os.environ.get('YTDLP_COOKIE_SECRET', '/etc/secrets/youtube_cookies.txt')
COOKIES_PATH = os.path.join(tempfile.gettempdir(), 'spotily_youtube_cookies.txt')
if os.path.exists(_COOKIE_SRC):
    try:
        shutil.copyfile(_COOKIE_SRC, COOKIES_PATH)
        os.chmod(COOKIES_PATH, 0o600)
        YTDLP_EXTRA += ['--cookies', COOKIES_PATH]
    except Exception as err:
        print(f'Cookie setup error: {err}', flush=True)

# Streamed songs are cached on disk so the server can serve HTTP Range
# requests (enabling seek) while keeping the audio same-origin (so the
# Web Audio EQ / normalization pipeline in the app still works).
#
# All server-side storage lives OUTSIDE the project directory — the OS temp
# folder (auto-cleaned on reboot) — so the source folder stays clean.
_CACHE_ROOT = os.path.join(tempfile.gettempdir(), 'spotily_cache')
CACHE_DIR = os.path.join(_CACHE_ROOT, 'stream')
MAX_CACHE_FILES = 20

# Client playback log (POSTed from logger.js) — tail this to see what the
# phone is doing when a song fails to play.
_CLIENT_LOG_PATH = os.path.join(_CACHE_ROOT, 'client.log')


def _ensure_cache_root():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except Exception:
        pass


def _debug(msg):
    try:
        print(f'[DBG] {time.strftime("%Y-%m-%d %H:%M:%S")} {msg}', flush=True)
    except Exception:
        pass

# In-flight background downloads keyed by their `.part` path, so concurrent
# requests for the same track share one download instead of duplicating work.
_ACTIVE_DOWNLOADS = {}
_DL_LOCK = threading.Lock()
# Free-tier containers are memory-tight (512 MB): yt-dlp + the Deno JS runtime
# for YouTube challenge solving can peak near ~300 MB each, so only ever run
# one download at a time to stay under the OOM killer's threshold.
_DL_SEM = threading.Semaphore(1)

# Spotify's embed page still server-renders the full track list for public
# playlists/charts, so no login or API credentials are needed.
_SPOTIFY_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
               'Chrome/120.0 Safari/537.36')
_SPOTIFY_CACHE = {}
_SPOTIFY_TTL = 6 * 3600

# Per-quality size/duration cache (video_id, fmt) -> (duration, size, ts) for
# the download-quality picker. Populated by /api/info via `yt-dlp -g` clen/dur.
_INFO_CACHE = {}
_INFO_TTL = 6 * 3600

# Playlist id extraction (open.spotify.com, play.spotify.com, intl-* prefixes,
# embed pages, and spotify:playlist: URIs). Ids are 22-char base62.
_PLAYLIST_RE = re.compile(
    r'(?:spotify:playlist:|/embed/playlist/|/(?:intl-[a-z0-9-]+/)?playlist/)([A-Za-z0-9]{15,})'
)
_PROFILE_RE = re.compile(r'/(?:user|profile|intl-[a-z0-9-]+/user)/')

# Server-side cache for the "Spotify track -> YouTube result" resolver, so
# re-importing the same charts/playlists is instant instead of re-searching.
_SP_RESOLVE = {}
_SP_RESOLVE_PATH = os.path.join(CACHE_DIR, '_spresolve.json')

# Optional Spotify login (OAuth). Credentials live in a small server-side
# config file (the client secret must never reach the browser), tokens in
# another file with refresh support.
_SPOTIFY_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'spotify_config.json')
_SPOTIFY_TOKENS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'spotify_tokens.json')
_SPOTIFY_SCOPES = 'playlist-read-private user-library-read user-follow-read'

def _load_sp_resolve():
    if _SP_RESOLVE:
        return
    try:
        with open(_SP_RESOLVE_PATH) as f:
            _SP_RESOLVE.update(json.load(f))
    except Exception:
        pass

class MusicServerHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # CORS on every response: the client app may live on a different
        # origin than this API (e.g. hosted Render API + local app).
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Range')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        
        # API: Health (host uptime checks / cold-start warmup)
        if parsed.path == '/api/health':
            self.send_json({'ok': True, 'ts': time.time()})
            return

        # API: Search
        if parsed.path == '/api/search':
            query_params = urllib.parse.parse_qs(parsed.query)
            q = query_params.get('q', [''])[0].strip()
            if not q:
                self.send_json([])
                return
            tracks = self.search_tracks(q)
            self.send_json(tracks)
            return

        # API: Spotify playlist import (no login needed)
        elif parsed.path == '/api/spotify':
            query_params = urllib.parse.parse_qs(parsed.query)
            url = query_params.get('url', [''])[0].strip()
            result = self.fetch_spotify(url)
            if isinstance(result, str):
                self.send_json({'error': result})
            else:
                self.send_json(result)
            return

        # API: Spotify track -> YouTube resolver (server-cached)
        elif parsed.path == '/api/spresolve':
            query_params = urllib.parse.parse_qs(parsed.query)
            q = query_params.get('q', [''])[0].strip()
            n = query_params.get('n', ['5'])[0].strip()
            self.send_json(self.spresolve(q, n))
            return

        # API: YouTube playlist / single-video import
        elif parsed.path == '/api/ytplaylist':
            query_params = urllib.parse.parse_qs(parsed.query)
            url = query_params.get('url', [''])[0].strip()
            result = self.fetch_yt_playlist(url)
            if isinstance(result, str):
                self.send_json({'error': result})
            else:
                self.send_json(result)
            return

        # API: Spotify login (redirect to Spotify OAuth)
        elif parsed.path == '/api/spotify/login':
            self.spotify_login()
            return

        # API: Spotify OAuth callback
        elif parsed.path == '/api/spotify/callback':
            query_params = urllib.parse.parse_qs(parsed.query)
            code = query_params.get('code', [''])[0].strip()
            if not code:
                self.send_json({'error': 'Spotify login cancelled.'})
            else:
                self.spotify_callback(code)
            return

        # API: Spotify connected user (playlists / liked / albums / shows)
        elif parsed.path == '/api/spotify/me':
            self.send_json(self.fetch_spotify_me())
            return

        # API: Spotify user playlist tracks by id (needs login for private ones)
        elif parsed.path == '/api/spotify/playlist':
            query_params = urllib.parse.parse_qs(parsed.query)
            pid = query_params.get('id', [''])[0].strip()
            if not pid:
                self.send_json({'error': 'Missing playlist id'})
            else:
                self.send_json(self.fetch_spotify_user_playlist(pid))
            return

        # API: Spotify config / connection status
        elif parsed.path == '/api/spotify/config_status':
            self.send_json(self.spotify_config_status())
            return

        # API: Spotify disconnect
        elif parsed.path == '/api/spotify/logout':
            self.send_json(self.spotify_logout())
            return

        # API: Audio Stream
        elif parsed.path == '/api/stream':
            query_params = urllib.parse.parse_qs(parsed.query)
            video_id = query_params.get('id', [''])[0].strip()
            if not video_id:
                self.send_error(400, 'Missing video id')
                return
            fmt = query_params.get('format', ['High'])[0].strip()
            player = query_params.get('player', [''])[0].strip()
            self.stream_audio(video_id, fmt, player=player)
            return

        # Download: waits for the FULL file then serves it (for offline saving).
        elif parsed.path == '/api/download':
            query_params = urllib.parse.parse_qs(parsed.query)
            video_id = query_params.get('id', [''])[0].strip()
            if not video_id:
                self.send_error(400, 'Missing video id')
                return
            fmt = query_params.get('format', ['High'])[0].strip()
            player = query_params.get('player', [''])[0].strip()
            self.download_audio(video_id, fmt, player=player)
            return

        # Preload: start the background download early (next-track warm-ahead),
        # returns instantly so playback feels instant when the track is tapped.
        elif parsed.path == '/api/preload':
            query_params = urllib.parse.parse_qs(parsed.query)
            video_id = query_params.get('id', [''])[0].strip()
            if not video_id:
                self.send_json({'error': 'Missing video id'})
                return
            fmt = query_params.get('format', ['High'])[0].strip()
            player = query_params.get('player', [''])[0].strip()
            self.preload_audio(video_id, fmt, player=player)
            return

        # Info: per-quality sizes/duration for the download picker.
        elif parsed.path == '/api/info':
            query_params = urllib.parse.parse_qs(parsed.query)
            video_id = query_params.get('id', [''])[0].strip()
            if not video_id:
                self.send_json({'error': 'Missing video id'})
                return
            self.send_info(video_id)
            return

        # Client playback log tail (verification aid).
        elif parsed.path == '/api/log':
            self.send_client_log_tail()
            return
        
        # Serve static files (index.html, style.css, etc.)
        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/log':
            self.receive_client_log()
            return
        if parsed.path == '/api/spotify/config':
            try:
                n = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(n) if n else b'{}'
                body = json.loads(raw.decode('utf-8') or '{}')
                cfg = {k: (body.get(k) or '').strip()
                       for k in ('client_id', 'client_secret', 'redirect_uri')}
                with open(_SPOTIFY_CONFIG_PATH, 'w') as f:
                    json.dump(cfg, f)
                self.send_json({'ok': True})
            except Exception as err:
                print('Config save error:', err)
                self.send_json({'error': 'Could not save Spotify config'})
            return
        self.send_json({'error': 'Not found'})

    def receive_client_log(self):
        # Append batched client-side log lines (from logger.js) to client.log.
        try:
            n = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(n) if n else b'{}'
            body = json.loads(raw.decode('utf-8') or '{}')
            logs = body.get('logs') or []
            ua = body.get('ua') or ''
            if logs or ua:
                _ensure_cache_root()
            if logs:
                with open(_CLIENT_LOG_PATH, 'a', encoding='utf-8') as f:
                    for e in logs:
                        f.write("{ts} [{lvl}] {msg} {data}\n".format(
                            ts=e.get('ts', time.strftime('%Y-%m-%d %H:%M:%S')),
                            lvl=e.get('level', 'info'),
                            msg=e.get('msg', ''),
                            data=e.get('data', '') or ''))
            if ua:
                with open(_CLIENT_LOG_PATH, 'a', encoding='utf-8') as f:
                    f.write("{ts} [info] ua={ua}\n".format(
                        ts=time.strftime('%Y-%m-%d %H:%M:%S'), ua=ua))
            self.send_json({'ok': True})
        except Exception as err:
            print('Client log error:', err)
            self.send_json({'ok': False})

    def send_client_log_tail(self):
        try:
            lines = []
            if os.path.exists(_CLIENT_LOG_PATH):
                with open(_CLIENT_LOG_PATH, 'r', encoding='utf-8') as f:
                    lines = f.read().splitlines()[-200:]
            self.send_json({'lines': lines})
        except Exception as err:
            print('Client log tail error:', err)
            self.send_json({'lines': []})

    def send_json(self, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def search_tracks(self, query):
        # Retry once: yt-dlp/YouTube occasionally drops a search with a
        # transient failure or empty result (looks like "no results" to the user).
        for attempt in range(2):
            try:
                cmd = [
                    'yt-dlp',
                    f'ytsearch15:{query}',
                    '--dump-single-json',
                    '--flat-playlist',
                    '--no-warnings',
                    *YTDLP_EXTRA
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
                if res.returncode != 0:
                    if attempt == 0:
                        continue
                    return []

                data = json.loads(res.stdout)
                entries = data.get('entries', [])
                if not entries and attempt == 0:
                    continue

                tracks = []
                for e in entries:
                    vid = e.get('id')
                    if not vid:
                        continue
                    title = e.get('title', 'Unknown Track')
                    raw_artist = e.get('uploader') or e.get('channel') or 'Artist'
                    artist = raw_artist.replace('- Topic', '').replace('VEVO', '').strip()
                    dur = int(e.get('duration') or 180)

                    thumb_url = f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'

                    tracks.append({
                        'id': f'yt_{vid}',
                        'title': title,
                        'artist': artist,
                        'album': '',
                        'duration': dur,
                        'previewUrl': f'/api/stream?id={vid}',
                        'coverSmall': thumb_url,
                        'coverLarge': thumb_url,
                        'source': 'YouTube',
                    })
                return tracks
            except Exception as err:
                print('Search error:', err)
                if attempt == 0:
                    continue
        return []

    # ─── YouTube playlist import ────────────────────────────────────────────────
    def fetch_yt_playlist(self, url):
        try:
            if not url:
                return 'Missing YouTube URL'
            url = url.strip()
            if not url.startswith('http'):
                # Bare id (PL... / OLAK... / RD... / watch?v=...) -> build a URL
                url = f'https://www.youtube.com/playlist?list={url}'
            if 'youtu' not in url and 'youtube.com' not in url:
                return 'Not a YouTube link'
            # Flat extraction is fast (single request, no per-video fetch) and
            # still gives us id/title for every entry.
            cmd = ['yt-dlp', url, '--flat-playlist', '--dump-single-json',
                   '--no-warnings', '--skip-download', *YTDLP_EXTRA]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode != 0:
                return 'Could not load that YouTube link'
            data = json.loads(res.stdout)
            entries = data.get('entries') or []
            # A plain watch URL yields one top-level entry (no `entries` key).
            if not entries and data.get('id'):
                entries = [data]
            # Cap huge playlists so the client stays responsive.
            entries = entries[:500]
            if not entries:
                return 'No videos found in that playlist'
            name = data.get('title') or 'YouTube Playlist'
            tracks = []
            for e in entries:
                vid = e.get('id')
                if not vid:
                    continue
                title = e.get('title') or 'Untitled'
                dur = int(e.get('duration') or 0)
                thumb = f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
                tracks.append({
                    'id': f'yt_{vid}',
                    'title': title,
                    'artist': (e.get('uploader') or e.get('channel') or 'YouTube')
                              .replace('- Topic', '').strip(),
                    'album': name,
                    'duration': dur,
                    'previewUrl': f'/api/stream?id={vid}',
                    'coverSmall': thumb,
                    'coverLarge': thumb,
                    'source': 'YouTube',
                })
            return {'name': name, 'trackCount': len(tracks), 'tracks': tracks}
        except Exception as err:
            print('YT playlist error:', err)
            return 'Could not load that YouTube link'

    # ─── Spotify import ─────────────────────────────────────────────────────────
    def fetch_spotify(self, url):
        try:
            if not url:
                return 'Missing Spotify URL'
            url = url.strip()
            m = _PLAYLIST_RE.search(url)
            # Only follow redirects when the pasted link has no playlist id in
            # it already (e.g. spotify.link short links) — saves ~3s per call.
            if not m:
                resolved = self._follow_redirects(url) or url
                m = _PLAYLIST_RE.search(resolved)
            if not m:
                if _PROFILE_RE.search(url) or 'open.spotify.com' not in url:
                    return ('Spotify profiles need a login \u2014 paste a playlist '
                            'link from that profile instead.')
                return 'That is not a Spotify playlist link.'
            pid = m.group(1)
            cached = _SPOTIFY_CACHE.get(pid)
            if cached and time.time() - cached[0] < _SPOTIFY_TTL:
                return cached[1]
            data = self._scrape_spotify_playlist(pid)
            if isinstance(data, str):
                return data
            _SPOTIFY_CACHE[pid] = (time.time(), data)
            return data
        except Exception as err:
            print('Spotify fetch error:', err)
            return 'Spotify fetch failed'

    def _follow_redirects(self, url):
        try:
            cmd = ['curl', '-sL', '--max-time', '20', '-A', _SPOTIFY_UA,
                   '-o', '/dev/null', '-w', '%{url_effective}', url]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass
        return None

    def _scrape_spotify_playlist(self, pid):
        url = f'https://open.spotify.com/embed/playlist/{pid}'
        for attempt in range(3):
            try:
                res = subprocess.run(
                    ['curl', '-sL', '--max-time', '30', '-A', _SPOTIFY_UA, url],
                    capture_output=True, text=True, timeout=35)
            except Exception as err:
                print('Spotify scrape error:', err)
                return 'Could not load the Spotify playlist'
            data = None
            if res.returncode == 0 and res.stdout:
                data = self._parse_spotify_embed(res.stdout)
            if data:
                return data
            # Transient failure (rate-limit / bot check / geo miss) — back off
            # and retry before giving up.
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        return 'Could not read the Spotify playlist'

    def _parse_spotify_embed(self, html):
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                      html, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
            ent = data['props']['pageProps']['state']['data']['entity']
        except Exception:
            return None
        if not ent or not ent.get('trackList'):
            return None
        name = ent.get('name') or ent.get('title') or 'Spotify Playlist'
        cover = ''
        try:
            cover = ent['coverArt']['sources'][0]['url']
        except Exception:
            pass
        tracks = []
        for it in ent.get('trackList') or []:
            title = (it.get('title') or '').replace('\xa0', ' ').strip()
            if not title:
                continue
            sub = it.get('subtitle')
            if isinstance(sub, str):
                artist = sub.replace('\xa0', ' ').strip()
            else:
                try:
                    artist = ', '.join((x.get('name', '') or '').replace('\xa0', ' ')
                                       for x in (sub or []) if x.get('name'))
                except Exception:
                    artist = ''
            uri = it.get('uri') or ''
            spid = uri.split(':')[-1] if uri else ''
            dur_ms = it.get('duration') or 0
            tracks.append({
                'id': f'sp_{spid}',
                'spUri': uri,
                'spId': spid,
                'title': title,
                'artist': artist or 'Spotify',
                'album': '',
                'duration': int(dur_ms / 1000) if dur_ms else 0,
                'previewUrl': None,
                'coverSmall': cover,
                'coverLarge': cover,
                'source': 'Spotify',
            })
        return {'name': name, 'cover': cover, 'trackCount': len(tracks), 'tracks': tracks}

    # ─── Spotify track → YouTube resolver (server-cached) ──────────────────────
    def spresolve(self, q, n=5):
        n = min(max(int(n) if str(n).isdigit() else 5, 1), 10)
        key = f'{n}|{" ".join(q.lower().split())}'
        if not key:
            return {'results': []}
        _load_sp_resolve()
        if key in _SP_RESOLVE:
            return {'cached': True, 'results': _SP_RESOLVE[key]}
        cmd = ['yt-dlp', f'ytsearch{n}:{q}', '--dump-single-json', '--flat-playlist',
               '--no-warnings', '--no-cache-dir', *YTDLP_EXTRA]
        results = []
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=90).decode()
            j = json.loads(out)
            for e in (j.get('entries') or []):
                vid = e.get('id')
                if not vid:
                    continue
                results.append({
                    'videoId': vid,
                    'title': e.get('title') or '',
                    'artist': (e.get('channel') or e.get('uploader') or '')
                              .replace(' - Topic', '').strip(),
                })
        except Exception as err:
            print('spresolve error:', err)
        _SP_RESOLVE[key] = results
        try:
            with open(_SP_RESOLVE_PATH, 'w') as f:
                json.dump(_SP_RESOLVE, f)
        except Exception:
            pass
        return {'cached': False, 'results': results}

    # ─── Optional Spotify login (OAuth) ────────────────────────────────────────
    def _spotify_config(self):
        try:
            with open(_SPOTIFY_CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            return None

    def _spotify_token(self):
        try:
            with open(_SPOTIFY_TOKENS_PATH) as f:
                return json.load(f)
        except Exception:
            return None

    def _save_token(self, tok):
        try:
            with open(_SPOTIFY_TOKENS_PATH, 'w') as f:
                json.dump(tok, f)
        except Exception as err:
            print('Token save error:', err)

    def _spotify_auth_header(self):
        cfg = self._spotify_config()
        if not cfg or not cfg.get('client_id') or not cfg.get('client_secret'):
            return None
        b64 = base64.b64encode(
            f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
        return f'Basic {b64}'

    def _token_request(self, form):
        auth = self._spotify_auth_header()
        if not auth:
            return None
        try:
            res = subprocess.run(
                ['curl', '-s', '--max-time', '20', '-X', 'POST',
                 'https://accounts.spotify.com/api/token',
                 '-H', f'Authorization: {auth}',
                 '-H', 'Content-Type: application/x-www-form-urlencoded',
                 '--data', form],
                capture_output=True, text=True, timeout=25)
            if res.returncode == 0 and res.stdout:
                return json.loads(res.stdout)
        except Exception:
            pass
        return None

    def _ensure_token(self):
        tok = self._spotify_token()
        if not tok or not tok.get('access_token'):
            return None
        if tok.get('expires_at', 0) > time.time():
            return tok['access_token']
        ref = tok.get('refresh_token')
        if not ref:
            return None
        form = urllib.parse.urlencode({'grant_type': 'refresh_token', 'refresh_token': ref})
        data = self._token_request(form)
        if data and data.get('access_token'):
            tok['access_token'] = data['access_token']
            tok['expires_at'] = time.time() + int(data.get('expires_in', 3600)) - 60
            if data.get('refresh_token'):
                tok['refresh_token'] = data['refresh_token']
            self._save_token(tok)
            return tok['access_token']
        return None

    def _spotify_api(self, path):
        token = self._ensure_token()
        if not token:
            return None
        try:
            res = subprocess.run(
                ['curl', '-s', '--max-time', '30',
                 '-H', f'Authorization: Bearer {token}',
                 f'https://api.spotify.com/v1{path}'],
                capture_output=True, text=True, timeout=35)
            if res.returncode == 0 and res.stdout:
                data = json.loads(res.stdout)
                return None if data.get('error') else data
        except Exception:
            pass
        return None

    def _spotify_api_paged(self, path, limit=50, max_total=300):
        items, offset = [], 0
        while offset < max_total:
            sep = '&' if '?' in path else '?'
            data = self._spotify_api(f'{path}{sep}limit={limit}&offset={offset}')
            if not data:
                break
            chunk = data.get('items') or []
            items.extend(chunk)
            if not chunk:
                break
            offset += len(chunk)
            if offset >= (data.get('total') or 0):
                break
        return items

    def _map_api_track(self, item):
        try:
            t = item.get('track') or item
            if not t or not t.get('id'):
                return None
            artists = ', '.join((a.get('name') or '') for a in (t.get('artists') or []))
            images = (t.get('album') or {}).get('images') or []
            cover = images[0].get('url', '') if images else ''
            return {
                'id': 'sp_' + t['id'],
                'spUri': t.get('uri') or f"spotify:track:{t['id']}",
                'spId': t['id'],
                'title': t.get('name') or '',
                'artist': artists or 'Spotify',
                'album': (t.get('album') or {}).get('name') or '',
                'duration': int((t.get('duration_ms') or 0) / 1000),
                'previewUrl': None,
                'coverSmall': cover,
                'coverLarge': cover,
                'source': 'Spotify',
            }
        except Exception:
            return None

    def spotify_login(self):
        cfg = self._spotify_config()
        if not cfg or not cfg.get('client_id') or not cfg.get('client_secret'):
            self.send_json({'error': 'Not configured',
                            'msg': 'Set your Spotify Developer credentials in Settings first.'})
            return
        redirect_uri = cfg.get('redirect_uri') or f'http://localhost:{PORT}/api/spotify/callback'
        params = urllib.parse.urlencode({
            'client_id': cfg['client_id'],
            'response_type': 'code',
            'scope': _SPOTIFY_SCOPES,
            'redirect_uri': redirect_uri,
            'show_dialog': 'false',
        })
        self.send_response(302)
        self.send_header('Location', f'https://accounts.spotify.com/authorize?{params}')
        self.end_headers()

    def spotify_callback(self, code):
        cfg = self._spotify_config()
        redirect_uri = cfg.get('redirect_uri') or f'http://localhost:{PORT}/api/spotify/callback'
        form = urllib.parse.urlencode({
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri,
        })
        data = self._token_request(form)
        if not data or not data.get('access_token'):
            self.send_json({'error': 'Spotify login failed.'})
            return
        tok = {
            'access_token': data['access_token'],
            'refresh_token': data.get('refresh_token', ''),
            'expires_at': time.time() + int(data.get('expires_in', 3600)) - 60,
            'user': '',
        }
        self._save_token(tok)
        me = self._spotify_api('/me')
        if me:
            tok['user'] = me.get('display_name') or me.get('id') or ''
            self._save_token(tok)
        self.send_response(302)
        self.send_header('Location', '/?sp_connected=1')
        self.end_headers()

    def spotify_config_status(self):
        cfg = self._spotify_config()
        tok = self._spotify_token()
        return {
            'configured': bool(cfg and cfg.get('client_id') and cfg.get('client_secret')),
            'connected': bool(tok and tok.get('access_token')),
            'user': (tok or {}).get('user', ''),
        }

    def spotify_logout(self):
        try:
            os.remove(_SPOTIFY_TOKENS_PATH)
        except OSError:
            pass
        return {'ok': True}

    def fetch_spotify_me(self):
        me = self._spotify_api('/me')
        if not me:
            return {'error': 'Not connected to Spotify. Log in from Settings first.'}
        playlists = self._spotify_api_paged('/me/playlists', max_total=300)
        liked = self._spotify_api_paged('/me/tracks', max_total=300)
        albums = self._spotify_api_paged('/me/albums', max_total=300)
        shows = self._spotify_api_paged('/me/shows', max_total=300)
        plist = []
        for p in playlists:
            if not p.get('id'):
                continue
            images = p.get('images') or []
            plist.append({
                'id': p['id'],
                'name': p.get('name') or 'Playlist',
                'trackCount': (p.get('tracks') or {}).get('total', 0),
                'cover': images[0].get('url', '') if images else '',
            })
        return {
            'user': me.get('display_name') or me.get('id') or 'Spotify',
            'playlists': plist,
            'liked': [t for t in (self._map_api_track(x) for x in liked) if t],
            'albums': [{'id': a.get('id'), 'name': a.get('name') or '',
                        'cover': ((a.get('images') or [{}])[0].get('url') or ''),
                        'trackCount': 0, 'tracks': []}
                       for a in albums if a.get('id')],
            'shows': [{'id': s.get('id'), 'name': s.get('name') or '',
                       'cover': ((s.get('images') or [{}])[0].get('url') or '')}
                      for s in shows if s.get('id')],
        }

    def fetch_spotify_user_playlist(self, pid):
        info = self._spotify_api(f'/playlists/{pid}')
        if not info:
            return {'error': 'Could not open that playlist (private or not found).'}
        tracks_raw = self._spotify_api_paged(f'/playlists/{pid}/tracks', max_total=300)
        tracks = [t for t in (self._map_api_track(x) for x in tracks_raw) if t]
        images = info.get('images') or []
        return {
            'name': info.get('name') or 'Playlist',
            'cover': images[0].get('url', '') if images else '',
            'trackCount': len(tracks),
            'tracks': tracks,
        }

    # yt-dlp format selectors per requested quality level
    FORMAT_SELECTORS = {
        'Low':    'ba/b[abr<=64]/ba/b',
        'Medium': 'ba/b[abr<=128]/ba/b',
        'High':   'ba/b',
    }

    def stream_audio(self, video_id, fmt='High', player=''):
        # Deterministic playback: the whole file is downloaded (background
        # yt-dlp) then served with Range support. Progressive byte-streaming
        # through the Cloudflare tunnel silently failed on phones (206 chunk
        # races), so playback now waits for the complete file — it is small
        # (3-6 MB opus) and `/api/preload` kicks off downloads ahead of play,
        # so the wait is usually zero and at most a few seconds.
        self._download_then_serve(video_id, fmt, player)

    def _download_then_serve(self, video_id, fmt, player=''):
        rng = self.headers.get('Range')
        _debug(f'STREAM id={video_id} fmt={fmt} rng={rng!r} ua={self.headers.get("User-Agent", "")[:40]}')
        try:
            selector = self.FORMAT_SELECTORS.get(fmt, self.FORMAT_SELECTORS['High'])
            self._purge_cache()
            os.makedirs(CACHE_DIR, exist_ok=True)
            final = os.path.join(CACHE_DIR, f'{video_id}_{fmt}.webm')
            part = final + '.part'

            # 1) Already fully cached -> serve instantly with Range support.
            if os.path.exists(final) and os.path.getsize(final) > 1024:
                _debug(f'CACHE HIT id={video_id} fmt={fmt}')
                self._serve_file_with_range(final)
                return

            # 2) Start the background download, wait for the full file (yt-dlp
            #    renames `.part` -> final when done), then serve it whole.
            url = f'https://www.youtube.com/watch?v={video_id}'
            self._start_download(part, url, selector, player=player)
            _debug(f'CACHE MISS id={video_id} fmt={fmt} waiting for download')

            t0 = time.time()
            deadline = t0 + 180
            while time.time() < deadline:
                if os.path.exists(final) and os.path.getsize(final) > 1024:
                    _debug(f'SERVE id={video_id} fmt={fmt} waited={time.time() - t0:.1f}s size={os.path.getsize(final)}')
                    self._serve_file_with_range(final)
                    return
                with _DL_LOCK:
                    active = part in _ACTIVE_DOWNLOADS
                if not active:
                    break
                time.sleep(0.25)
            _debug(f'STREAM FAILED id={video_id} fmt={fmt} waited={time.time() - t0:.1f}s active={active} final_exists={os.path.exists(final)}')
            self.send_error(502, 'Stream unavailable')
        except Exception as err:
            _debug(f'STREAM EXC id={video_id} fmt={fmt} err={err!r}')
            try:
                self.send_error(502, 'Stream unavailable')
            except Exception:
                pass

    def preload_audio(self, video_id, fmt='High', player=''):
        # Fire-and-forget: start the background yt-dlp download if it isn't
        # cached yet, so the next play request gets the file immediately.
        try:
            selector = self.FORMAT_SELECTORS.get(fmt, self.FORMAT_SELECTORS['High'])
            final = os.path.join(CACHE_DIR, f'{video_id}_{fmt}.webm')
            part = final + '.part'
            if os.path.exists(final) and os.path.getsize(final) > 1024:
                self.send_json({'ok': True, 'cached': True})
                return
            url = f'https://www.youtube.com/watch?v={video_id}'
            self._start_download(part, url, selector, player=player)
            self.send_json({'ok': True, 'cached': False})
        except Exception as err:
            print('Preload error:', err)
            self.send_json({'error': 'preload failed'})

    def _ytdlp_extra_for(self, player):
        # Temporary debugging override: `?player=tv` etc. swaps the YouTube
        # player client for the current request (datacenter IPs are flagged
        # differently per client). 'default'/'empty' -> the configured set.
        if not player or player == 'default':
            return list(YTDLP_EXTRA)
        extra = [a for a in YTDLP_EXTRA if 'player_client' not in a]
        extra += ['--extractor-args', f'youtube:player_client={player}']
        return extra

    def _start_download(self, part_path, watch_url, selector, player=''):
        # yt-dlp downloads to `<final>.part` (= `part_path`) and renames it to
        # `final_path` on completion.
        final_path = part_path[:-5] if part_path.endswith('.part') else part_path + '.final'
        extra = self._ytdlp_extra_for(player)
        with _DL_LOCK:
            if part_path in _ACTIVE_DOWNLOADS:
                return
            _ACTIVE_DOWNLOADS[part_path] = True

        def worker():
            t0 = time.time()
            try:
                with _DL_SEM:
                    res = subprocess.run(
                        ['yt-dlp', '-f', selector, '--no-playlist', '--no-warnings',
                         '--concurrent-fragments', '4', *extra,
                         '-o', final_path, watch_url],
                        capture_output=True, text=True, timeout=300)
                    dur = time.time() - t0
                if res.returncode == 0:
                    _debug(f'DL OK {final_path} dur={dur:.1f}s')
                else:
                    err_tail = ' | '.join((res.stderr or '').strip().splitlines()[-3:])
                    _debug(f'DL FAIL rc={res.returncode} dur={dur:.1f}s {final_path} err={err_tail}')
            except Exception as err:
                _debug(f'DL EXC {final_path} err={err!r}')
            finally:
                # Drop leftovers from failed/interrupted downloads.
                if not os.path.exists(final_path) or os.path.getsize(final_path) <= 1024:
                    for p in (final_path, part_path):
                        try:
                            if os.path.exists(p):
                                os.remove(p)
                        except OSError:
                            pass
                with _DL_LOCK:
                    _ACTIVE_DOWNLOADS.pop(part_path, None)

        threading.Thread(target=worker, daemon=True).start()

    def _purge_cache(self):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            now = time.time()
            files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)]
            # Drop anything older than 24h, then keep only the newest files.
            # Never touch in-progress downloads (`.part`).
            recent = []
            for p in files:
                try:
                    if p.endswith('.part'):
                        continue
                    if now - os.path.getmtime(p) > 24 * 3600:
                        os.remove(p)
                    else:
                        recent.append(p)
                except OSError:
                    pass
            recent.sort(key=os.path.getmtime, reverse=True)
            for p in recent[MAX_CACHE_FILES:]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception as err:
            print('Cache purge error:', err)

    def _serve_file_with_range(self, path):
        size = os.path.getsize(path)
        rng = self.headers.get('Range')
        status = 200
        start, end = 0, size - 1
        if rng:
            m = re.match(r'bytes=(\d*)-(\d*)', rng)
            if m:
                start = int(m.group(1)) if m.group(1) else 0
                end = int(m.group(2)) if m.group(2) else size - 1
                if start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206
        self.send_response(status)
        self.send_header('Content-Type', 'audio/webm')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        try:
            with open(path, 'rb') as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_info(self, video_id):
        # Resolve size/duration for High/Medium/Low in parallel (each runs a
        # `yt-dlp -g`, cached for 6h). Never delays a stream — only the menu.
        selectors = {q: self.FORMAT_SELECTORS[q] for q in ('High', 'Medium', 'Low')}
        out = {}
        def resolve(fmt, sel):
            out[fmt] = self._fmt_info(video_id, fmt, sel)
        threads = [threading.Thread(target=resolve, args=(f, s)) for f, s in selectors.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=45)
        self.send_json({
            'id': video_id,
            'formats': [out.get(q) for q in ('High', 'Medium', 'Low')],
        })

    def _fmt_info(self, video_id, fmt, selector):
        key = (video_id, fmt)
        hit = _INFO_CACHE.get(key)
        if hit and time.time() - hit[2] < _INFO_TTL:
            return {'quality': fmt, 'size': hit[1], 'duration': hit[0]}
        try:
            url = f'https://www.youtube.com/watch?v={video_id}'
            cmd = ['yt-dlp', '-f', selector, '--no-playlist', '--no-warnings', '-g', url, *YTDLP_EXTRA]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
            direct = res.stdout.strip().splitlines()[0] if res.returncode == 0 and res.stdout.strip() else ''
            if not direct:
                return {'quality': fmt, 'size': None, 'duration': None}
            clen, dur = 0, 0.0
            m = re.search(r'[?&]clen=(\d+)', direct)
            if m:
                clen = int(m.group(1))
            m = re.search(r'[?&]dur=([\d.]+)', direct)
            if m:
                dur = float(m.group(1))
            if clen > 0:
                _INFO_CACHE[key] = (dur, clen, time.time())
            return {'quality': fmt, 'size': clen or None, 'duration': dur or None}
        except Exception as err:
            print('Info resolve failed:', err)
            return {'quality': fmt, 'size': None, 'duration': None}

    def download_audio(self, video_id, fmt='High', player=''):
        # Deterministic full-file download for offline saving: start the
        # background yt-dlp download, wait for it to complete (renames `.part`
        # -> final), then serve the whole cached file with Range support.
        try:
            selector = self.FORMAT_SELECTORS.get(fmt, self.FORMAT_SELECTORS['High'])
            self._purge_cache()
            os.makedirs(CACHE_DIR, exist_ok=True)
            final = os.path.join(CACHE_DIR, f'{video_id}_{fmt}.webm')
            part = final + '.part'

            if os.path.exists(final) and os.path.getsize(final) > 1024:
                self._serve_file_with_range(final)
                return

            url = f'https://www.youtube.com/watch?v={video_id}'
            self._start_download(part, url, selector, player=player)

            deadline = time.time() + 180
            while time.time() < deadline:
                if os.path.exists(final) and os.path.getsize(final) > 1024:
                    self._serve_file_with_range(final)
                    return
                with _DL_LOCK:
                    active = part in _ACTIVE_DOWNLOADS
                if not active:
                    break
                time.sleep(0.25)
            self.send_error(502, 'Stream unavailable')
        except Exception as err:
            print('Download error:', err)
            try:
                self.send_error(502, 'Stream unavailable')
            except Exception:
                pass

if __name__ == '__main__':
    # Threaded server: audio streams and search requests must run concurrently.
    # A single-threaded server would block every /api/search (including the
    # "Recommended for you" engine) while any song is streaming.
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    print(f' Starting Spotily pure yt-dlp Music Server on http://localhost:{PORT} ...')
    with http.server.ThreadingHTTPServer(('', PORT), MusicServerHandler) as httpd:
        httpd.serve_forever()
