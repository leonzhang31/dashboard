"""Cached F1 standings, race schedule and BBC headlines for the dashboard."""
import json
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import certifi

TRACK_ZONES = {
    'albert_park': 'Australia/Melbourne', 'shanghai': 'Asia/Shanghai',
    'suzuka': 'Asia/Tokyo', 'bahrain': 'Asia/Bahrain', 'jeddah': 'Asia/Riyadh',
    'miami': 'America/New_York', 'imola': 'Europe/Rome', 'monaco': 'Europe/Monaco',
    'villeneuve': 'America/Toronto', 'catalunya': 'Europe/Madrid',
    'madring': 'Europe/Madrid', 'madrid': 'Europe/Madrid',
    'red_bull_ring': 'Europe/Vienna', 'silverstone': 'Europe/London',
    'spa': 'Europe/Brussels', 'hungaroring': 'Europe/Budapest',
    'zandvoort': 'Europe/Amsterdam', 'monza': 'Europe/Rome',
    'baku': 'Asia/Baku', 'marina_bay': 'Asia/Singapore',
    'americas': 'America/Chicago', 'rodriguez': 'America/Mexico_City',
    'interlagos': 'America/Sao_Paulo', 'vegas': 'America/Los_Angeles',
    'las_vegas': 'America/Los_Angeles', 'losail': 'Asia/Qatar',
    'yas_marina': 'Asia/Dubai',
}
_cache = {}
_lock = threading.Lock()


def download(url):
    request = Request(url, headers={'User-Agent': 'SchoolDashboard/1.0'})
    with urlopen(request, timeout=8, context=ssl.create_default_context(cafile=certifi.where())) as response:
        return response.read(2_000_000)


def standings(kind, season):
    table = json.loads(download(f'https://api.jolpi.ca/ergast/f1/{season}/{kind}standings/'))['MRData']['StandingsTable']
    lists = table['StandingsLists']
    if not lists:
        return {'season': str(season), 'round': None, 'leaders': []}
    latest = lists[-1]
    field = 'DriverStandings' if kind == 'driver' else 'ConstructorStandings'
    leaders = []
    for item in latest[field][:3]:
        name = (item['Driver']['givenName'] + ' ' + item['Driver']['familyName']) if kind == 'driver' else item['Constructor']['name']
        leaders.append({'position': item['position'], 'name': name, 'points': item['points']})
    return {'season': latest['season'], 'round': latest['round'], 'leaders': leaders}


def race_schedule(season):
    races = json.loads(download(f'https://api.jolpi.ca/ergast/f1/{season}/races/?limit=100'))['MRData']['RaceTable']['Races']
    return races


def next_race(races, now):
    for race in sorted(races, key=lambda item: (item['date'], item.get('time', ''))):
        start = datetime.fromisoformat(race['date'] + 'T' + race.get('time', '23:59:59Z').replace('Z', '+00:00'))
        if start <= now:
            continue
        circuit = race['Circuit']
        zone = TRACK_ZONES.get(circuit['circuitId'])
        def label(tz):
            return start.astimezone(ZoneInfo(tz)).strftime('%a, %b %d · %I:%M %p %Z')
        return {
            'name': race['raceName'], 'circuit': circuit['circuitName'],
            'circuit_id': circuit['circuitId'],
            'location': f"{circuit['Location']['locality']}, {circuit['Location']['country']}",
            'start': start.isoformat() if race.get('time') else None,
            'track_time': label(zone) if zone and race.get('time') else 'Track time not published',
            'toronto_time': label('America/Toronto') if race.get('time') else f"{race['date']} · Start time not published",
        }
    return None


def headlines():
    root = ET.fromstring(download('https://feeds.bbci.co.uk/sport/formula1/rss.xml'))
    return [{'title': item.findtext('title', ''), 'url': item.findtext('link', ''),
             'published': item.findtext('pubDate', '')}
            for item in root.findall('./channel/item')[:4]
            if item.findtext('link', '').startswith(('https://www.bbc.', 'https://www.bbc.co.uk/'))]


def get_f1_info():
    now = datetime.now(timezone.utc)
    season = now.astimezone(ZoneInfo('America/Toronto')).year
    loaders = {'drivers': lambda: standings('driver', season),
               'constructors': lambda: standings('constructor', season),
               'schedule': lambda: race_schedule(season), 'news': headlines}
    with _lock:
        if _cache.get('season') != season:
            _cache.clear()
            _cache['season'] = season
        due = [key for key in loaders if time.monotonic() >= _cache.get(key, {}).get('retry_at', 0)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {key: pool.submit(loaders[key]) for key in due}
            for key, job in jobs.items():
                old = _cache.get(key, {})
                try:
                    _cache[key] = {'data': job.result(), 'updated': now.isoformat(),
                                   'stale': False, 'retry_at': time.monotonic() + 300}
                except (OSError, ValueError, KeyError, TypeError, ET.ParseError):
                    _cache[key] = {**old, 'stale': True, 'retry_at': time.monotonic() + 60}
        sections = {key: {field: value for field, value in _cache[key].items() if field != 'retry_at'} for key in loaders}
        schedule = sections.pop('schedule')
        sections['race'] = {**schedule, 'data': next_race(schedule.get('data', []), now)}
        return {'season': season, 'sections': sections}
