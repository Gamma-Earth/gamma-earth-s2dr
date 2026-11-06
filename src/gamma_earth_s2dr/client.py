"""Client for the Gamma Earth S2DR3 hosted API: submit a job, follow its STAC collection, download the products.

The flow is the one proven on stage (ticket T14): POST a job, poll the `stac_job` link it returns until the
collection holds the item, then fetch each asset through the API's redirect to a short-lived signed URL.

The API key is sent only to the API host (in the URL path for the job API, in the X-API-Key header for the STAC API)
and never to the signed download host. It is never printed or logged, is scrubbed from every error text, and no request
that carries it follows a redirect.
"""
import datetime as dt
import json
import math
import os
import re
import time
from importlib import metadata
from pathlib import Path
from urllib.parse import quote, urlsplit

import pystac
import requests


def _client_tag():
    """'gamma-earth-s2dr/<version>': the API counts jobs with this tag as made by the Python package (S2DR3P). None if the
    installed version cannot be read."""
    try:
        version = re.sub(r'[^A-Za-z0-9._-]', '', metadata.version('gamma-earth-s2dr'))[:12]
    except metadata.PackageNotFoundError:
        return None
    return f'gamma-earth-s2dr/{version}' if version else None


CLIENT_TAG = _client_tag()
DEFAULT_API_URL = 'https://api.gamma.earth'      # the public API; GE_API_URL or the api_url argument replace it
TIFF_MAGIC = (b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+')
TIMEOUT = 120
MAX_TRANSIENT = 5                   # consecutive transient errors (network, 5xx, 429) that wait() rides out
COMPLETED_GRACE = 6                 # polls (about two minutes) a completed job may take to show its product

JOB_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
PID = re.compile(r'T[0-9]{2}[A-Z]{3}-[A-Za-z0-9]{1,32}-[0-9]{8}')
FILE_PART = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,80}')       # an item id or asset name used in a file name


class GammaEarthError(Exception):
    """Anything that went wrong talking to the API. The text never contains the key."""


class TransientError(GammaEarthError):
    """A network error, a 5xx or a 429: worth trying again."""


class NeedsConfirmation(GammaEarthError):
    """Processing a scene spends credits and was not confirmed (or is above the limit given). `credits` is the price the
    API showed for the scene, or None when it could not tell."""
    def __init__(self, message, credits=None):
        super().__init__(message)
        self.credits = credits


class JobFailed(GammaEarthError):
    """The job ended in the `failed` state (its credits are released)."""


class JobTimeout(GammaEarthError):
    """The product was not there before the time ran out; the job may still be running."""


def _aoi_text(aoi):
    if isinstance(aoi, str):
        return ' '.join(aoi.split())
    values = list(aoi)
    if len(values) != 4:
        raise ValueError('aoi is a bounding box: lon_min lat_min lon_max lat_max')
    return ' '.join(str(float(v)) for v in values)


PRODUCT_COLLECTION = 's2dr3'
MAX_PAGES = 20                      # pages one search follows, whatever max_items says
MAX_NEXT_BODY_CHARS = 16384
TILE = re.compile(r'[0-9]{2}[A-Z]{3}')
DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
NUMBER = re.compile(r'-?[0-9]+(?:\.[0-9]+)?')
PROCESS_BODY_KEYS = {'aoi', 'date', 'date_range', 'max_clouds'}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _date_text(value, what):
    """A date, a datetime (taken as UTC when it has no zone), a text (checked by the API), or None as an open end."""
    if value is None:
        return '..'
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value.strftime('%Y-%m-%dT%H:%M:%SZ')
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    raise TypeError(f'{what} must be a date, a datetime or a text')


def _interval(value):
    if isinstance(value, (tuple, list)):
        if len(value) != 2 or value[0] is None and value[1] is None:
            raise ValueError('datetime as a pair needs a start and/or an end')
        return f'{_date_text(value[0], "datetime")}/{_date_text(value[1], "datetime")}'
    return _date_text(value, 'datetime')


def _bbox(value):
    parts = value.split(',') if isinstance(value, str) else value
    try:
        parts = list(parts)
    except TypeError:
        raise ValueError('bbox is four numbers: west, south, east, north') from None
    if len(parts) != 4:
        raise ValueError('bbox is four numbers: west, south, east, north')
    numbers = []
    for p in parts:
        if isinstance(p, str):
            try:
                p = float(p)
            except ValueError:
                raise ValueError('bbox is four numbers: west, south, east, north') from None
        if not _is_number(p):
            raise ValueError('bbox is four numbers: west, south, east, north')
        if not math.isfinite(p):
            raise ValueError('bbox is four numbers: west, south, east, north')
        numbers.append(float(p))
    return numbers


def _text_list(value, what):
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f'{what} is a list of texts')
    return list(value)


class SearchResult:
    """What a search found: the pystac items in the order the API gave them, split into the products you own (collection
    s2dr3) and the unprocessed Sentinel-2 scenes you could process, with the API's own notes."""

    def __init__(self, items, warnings=None, scenes_info=None, matched=None):
        self.items = list(items)
        self.warnings = list(warnings or [])
        self.scenes_info = scenes_info
        self.matched = matched

    @property
    def products(self):
        return [i for i in self.items if i.collection_id == PRODUCT_COLLECTION]

    @property
    def scenes(self):
        return [i for i in self.items if i.collection_id != PRODUCT_COLLECTION]

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]

    def __repr__(self):
        return f'SearchResult({len(self.products)} product(s), {len(self.scenes)} scene(s))'


class _KeyStacIO(pystac.StacIO):
    """Reads STAC documents of the API with the key in the header, for pystac. It reads nothing from any other host
    (the key must not go there) and writes nothing."""

    def __init__(self, client):
        super().__init__()
        self._client = client

    def read_text(self, source, *args, **kwargs):
        href = source if isinstance(source, str) else str(getattr(source, 'absolute_href', source))
        c = self._client
        if href != f'{c._api}/stac' and not c._own(href, 'stac'):
            raise GammaEarthError('refusing to read a STAC document that is not on the API host')
        r = c._stac_get(href)
        if r.status_code >= 500 or r.status_code == 429:
            raise TransientError(f'STAC document not available (HTTP {r.status_code})')
        if r.status_code != 200:
            raise GammaEarthError(f'STAC document not available (HTTP {r.status_code})')
        try:
            return json.dumps(r.json())
        except ValueError:
            raise GammaEarthError('a STAC document could not be read') from None

    def write_text(self, dest, txt, *args, **kwargs):
        raise GammaEarthError('this catalog is read-only')


class Client:
    """`Client(api_url, api_key)`. They default to the environment variables GE_API_URL and GE_API_KEY; without an address
    the public API, https://api.gamma.earth, is used. The key is always required."""

    def __init__(self, api_url=None, api_key=None, session=None):
        api_url = (api_url or os.environ.get('GE_API_URL') or DEFAULT_API_URL).rstrip('/')
        api_key = api_key or os.environ.get('GE_API_KEY') or ''
        if not api_url.startswith('https://'):
            raise ValueError('api_url (or GE_API_URL) must be an https URL')
        if not api_key:
            raise ValueError('api_key (or GE_API_KEY) is required')
        self._api = api_url
        self._key = api_key
        self._session = session or requests.Session()

    def __repr__(self):
        return f'Client({self._api!r}, api_key=<hidden>)'

    # --- plumbing -----------------------------------------------------------------------------------------------

    def _scrub(self, text):
        text = str(text)
        for form in (self._key, quote(self._key), quote(self._key, safe='')):
            text = text.replace(form, '<key>')
        return text

    def _request(self, method, url, **kwargs):
        """One request, never following a redirect (the key must not travel to another host). A 3xx therefore comes
        back as an ordinary non-200 answer, which every caller treats as a failure (the asset call reads its 302)."""
        kwargs.setdefault('timeout', TIMEOUT)
        kwargs['allow_redirects'] = False
        try:
            return self._session.request(method, url, **kwargs)
        except requests.RequestException as e:
            raise TransientError(f'{type(e).__name__}: {self._scrub(e)[:200]}') from None

    def _jobs_url(self, suffix=''):
        return f'{self._api}/jobs/{quote(self._key, safe="")}{suffix}'

    def _stac_get(self, url, **kwargs):
        return self._request('GET', url, headers={'X-API-Key': self._key}, **kwargs)

    def _own(self, link, prefix):
        """True if `link` is on our API under `prefix`: the key is never sent anywhere else."""
        return isinstance(link, str) and link.startswith(f'{self._api}/{prefix}/')

    # --- jobs ---------------------------------------------------------------------------------------------------

    def submit(self, aoi, date, date_range=7, max_clouds=30):
        """Create a job and return a Job. THIS SPENDS CREDITS. Do not retry blindly: if this raises after the request
        was sent (a timeout, a lost answer), the job may exist; look at your jobs before submitting again."""
        body = {'date': str(date), 'aoi': _aoi_text(aoi), 'date_range': int(date_range), 'max_clouds': int(max_clouds)}
        if CLIENT_TAG:
            body['client'] = CLIENT_TAG        # the API records the job as made by the Python package (S2DR3P)
        try:
            r = self._request('POST', self._jobs_url(), json=body)
        except GammaEarthError as e:
            raise GammaEarthError(f'{e} (the job may have been created: check your jobs before submitting again)') from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or not isinstance(data, dict) or 'job_id' not in data:
            error = data.get('error', '') if isinstance(data, dict) else ''
            raise GammaEarthError(f'the job was not created (HTTP {r.status_code}): {self._scrub(error)[:200]}')
        return Job(self, data)

    def job(self, job_id):
        """Follow a job that already exists."""
        return Job(self, {'job_id': str(job_id)})

    # --- search and process -------------------------------------------------------------------------------------

    def search(self, bbox=None, intersects=None, datetime=None, ids=None, collections=None, max_clouds=None, tile=None,
               query=None, limit=10, max_items=None):
        """Search your products (collection `s2dr3`) and, when you give an area, the unprocessed Sentinel-2 scenes of that
        area (collection `sentinel-2-c1-l2a`, on the first page only, at most 20, newest first). Returns a SearchResult.

        `bbox` is (west, south, east, north); `datetime` a date, a datetime, a text ('2024-06-01/2024-06-30') or a pair of
        them (None = open end); `ids` product ids; `max_clouds` the most cloud cover over YOUR AREA, as a job's
        max_clouds; `tile` e.g. '36UUB'; `limit` products per page; `max_items` follows the next pages until that many
        products (at most 20 pages). Nothing is spent: processing a scene is a separate, confirmed step (process())."""
        body = {}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('limit is a whole number from 1 to 100')
        body['limit'] = limit
        if max_items is not None and (isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1):
            raise ValueError('max_items is a whole number from 1')
        if bbox is not None and intersects is not None:
            raise ValueError('give bbox or intersects, not both')
        if bbox is not None:
            body['bbox'] = _bbox(bbox)
        if intersects is not None:
            if not isinstance(intersects, dict) or 'type' not in intersects or 'coordinates' not in intersects:
                raise ValueError('intersects is a GeoJSON geometry')
            body['intersects'] = intersects
        if datetime is not None:
            body['datetime'] = _interval(datetime)
        if ids is not None:
            body['ids'] = _text_list(ids, 'ids')
        if collections is not None:
            body['collections'] = _text_list(collections, 'collections')
        if max_clouds is not None:
            if not _is_number(max_clouds) or not 0 <= max_clouds <= 100:
                raise ValueError('max_clouds is a number from 0 to 100')
            body['max_clouds'] = float(max_clouds)
        if query is not None and not isinstance(query, dict):
            raise ValueError('query is a dict of STAC query conditions')
        conditions = dict(query or {})
        if tile is not None:
            if not isinstance(tile, str) or not TILE.fullmatch(tile):
                raise ValueError("tile looks like '36UUB'")
            if 'grid:code' in conditions:
                raise ValueError("give the tile or a 'grid:code' condition, not both")
            conditions['grid:code'] = {'eq': f'MGRS-{tile}'}
        if conditions:
            body['query'] = conditions

        features, warnings, scenes_info, matched, products = [], [], None, None, 0
        for page_number in range(MAX_PAGES):
            doc = self._search_page(body)
            if page_number == 0:
                warnings, scenes_info, matched = doc.get('warnings') or [], doc.get('s2dr3:scenes'), doc.get('numberMatched')
            for feature in doc['features']:
                features.append(feature)
                products += feature.get('collection') == PRODUCT_COLLECTION
            if max_items is None or products >= max_items:
                break
            body = self._next_body(doc)
            if body is None:
                break
        else:
            warnings = (warnings if isinstance(warnings, list) else []) + [f'stopped after {MAX_PAGES} pages: narrow the search to see the rest']
        items, kept_products = [], 0
        for feature in features:
            if feature.get('collection') == PRODUCT_COLLECTION:
                if max_items is not None and kept_products >= max_items:
                    continue
                kept_products += 1
            try:
                items.append(pystac.Item.from_dict(feature))
            except Exception:
                raise GammaEarthError('a search result could not be read') from None
        return SearchResult(items, warnings if isinstance(warnings, list) else [], scenes_info, matched)

    def _search_page(self, body):
        r = self._request('POST', f'{self._api}/stac/search', json=body, headers={'X-API-Key': self._key})
        try:
            doc = r.json()
        except ValueError:
            doc = None
        if r.status_code >= 500 or r.status_code == 429:
            raise TransientError(f'search not available (HTTP {r.status_code})')
        if r.status_code != 200:
            error = doc.get('error', '') if isinstance(doc, dict) else ''
            raise GammaEarthError(f'search refused (HTTP {r.status_code}): {self._scrub(error)[:200]}')
        if (not isinstance(doc, dict) or doc.get('type') != 'FeatureCollection' or not isinstance(doc.get('features'), list)
                or not all(isinstance(f, dict) for f in doc['features'])):
            raise GammaEarthError('the search answer is not a feature collection')
        return doc

    def _next_body(self, doc):
        """The body of the next page's POST, or None. Only a next link to this API's search with a small JSON body is followed."""
        for link in doc.get('links') or []:
            if isinstance(link, dict) and link.get('rel') == 'next':
                body = link.get('body')
                if (link.get('method') == 'POST' and link.get('href') == f'{self._api}/stac/search' and isinstance(body, dict)
                        and len(json.dumps(body)) <= MAX_NEXT_BODY_CHARS):
                    return body
        return None

    def process(self, item, confirm=False, max_credits=None):
        """Process an unprocessed scene from a search. THIS SPENDS CREDITS, so nothing is sent unless `confirm=True`; without
        it a NeedsConfirmation says what the API showed as the price. `max_credits` refuses a price above it (and a scene
        with no price shown). Returns a Job (follow it with job.wait() / job.download()).

        The job is made from the item's own `s2dr3:process` link after checking that it is the API's (same host, same
        shape): it carries the area and the DATE of the scene, and the job picks the scene by its usual rules. So the
        price shown here is the search's estimate for that scene; the job is charged by the job's own price rules."""
        data = item.to_dict() if isinstance(item, pystac.Item) else item
        props = data.get('properties') if isinstance(data, dict) else None
        if not isinstance(props, dict):
            raise GammaEarthError('this is not a scene from a search')
        if data.get('collection') == PRODUCT_COLLECTION:
            raise GammaEarthError('this is already one of your products: download it instead')
        if props.get('s2dr3:status') != 'unprocessed':
            raise GammaEarthError('this is not an unprocessed scene from a search')
        if props.get('s2dr3:processable') is False:
            raise GammaEarthError('this scene is too large for one job: search a smaller area')
        links = [l for l in data.get('links') or [] if isinstance(l, dict) and l.get('rel') == 's2dr3:process']
        if len(links) != 1:
            raise GammaEarthError('this scene has no process link')
        link = links[0]
        body = link.get('body')
        if (link.get('href') != f'{self._api}/jobs/{{api_key}}' or link.get('method') != 'POST' or not isinstance(body, dict)
                or set(body) != PROCESS_BODY_KEYS):
            raise GammaEarthError('the process link of this scene is not one of this API')
        aoi, day, date_range, max_clouds = body['aoi'], body['date'], body['date_range'], body['max_clouds']
        if (not isinstance(aoi, str) or len(aoi.split(' ')) != 4 or not all(NUMBER.fullmatch(p) for p in aoi.split(' '))
                or not isinstance(day, str) or not DATE.fullmatch(day)
                or isinstance(date_range, bool) or not isinstance(date_range, int) or not 0 <= date_range <= 90
                or not _is_number(max_clouds) or not 0 <= max_clouds <= 100):
            raise GammaEarthError('the process link of this scene has values this client does not accept')
        credits = props.get('s2dr3:estimated_credits')
        credits = math.ceil(credits) if _is_number(credits) and math.isfinite(credits) and credits >= 0 else None
        price = f'about {credits} credits' if credits is not None else 'an unknown number of credits (the API showed no price)'
        if max_credits is not None and (credits is None or credits > max_credits):
            raise NeedsConfirmation(f'processing this scene costs {price}, which is not within max_credits={max_credits}', credits)
        if not confirm:
            raise NeedsConfirmation(f'processing this scene costs {price}. Call process(scene, confirm=True) to go ahead', credits)
        return self.submit(aoi, day, date_range=date_range, max_clouds=max_clouds)

    # --- catalog ------------------------------------------------------------------------------------------------

    def stac_io(self):
        """A pystac StacIO that reads the API's STAC documents with your key (and nothing from any other host)."""
        return _KeyStacIO(self)

    def catalog(self):
        """The API's root as a pystac Catalog: `list(client.catalog().get_children())` gives the collection `s2dr3`."""
        return pystac.Catalog.from_file(f'{self._api}/stac', stac_io=self.stac_io())

    # --- download -----------------------------------------------------------------------------------------------

    def download(self, collection, dest, assets=None):
        """Download the assets of every item of a pystac ItemCollection into `dest`; returns the list of paths.
        `assets` is a list of asset names (for example ['tci']); None means all. A file is `<item id>_<asset>.tif`.
        Everything is checked before the first request, so a bad name never leaves a half-done download."""
        plan = []
        for item in collection:
            names = sorted(item.assets) if assets is None else list(assets)
            for name in names:
                if name not in item.assets:
                    raise GammaEarthError(f'item {item.id} has no asset {name!r} (it has {sorted(item.assets)})')
                if not (FILE_PART.fullmatch(str(item.id)) and FILE_PART.fullmatch(str(name))):
                    raise GammaEarthError('an item id or asset name is not safe to use in a file name')
                href = item.assets[name].href
                if not self._own(href, 'stac/assets'):
                    raise GammaEarthError('refusing an asset link that is not on the API host')
                plan.append((item, name, href, f'{item.id}_{name}.tif'))
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        paths = [self._download_asset(href, dest / filename) for _, _, href, filename in plan]
        self._write_collection_file(dest / 'collection.json', plan)
        return paths

    @staticmethod
    def _write_collection_file(path, plan):
        """`collection.json` next to the files: the downloaded items with only the downloaded assets, pointing at the local
        files by relative path. No links, so no API address and no key; written last and atomically, so a failed download
        leaves an earlier file alone."""
        if not plan:
            return
        features = {}
        try:    # an earlier download into the same folder stays in the listing
            for old in json.loads(path.read_text()).get('features', []):
                features[old['id']] = old
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            features = {}
        for item, name, _, filename in plan:
            feature = features.get(item.id)
            if feature is None:
                feature = item.to_dict(include_self_link=False, transform_hrefs=False)
                feature['links'] = []
                feature.pop('collection', None)
                feature['assets'] = {}
                features[item.id] = feature
            # Only these asset fields are kept: nothing that could hold an API or storage address.
            source = item.assets[name].to_dict()
            asset = {k: source[k] for k in ('type', 'title', 'description', 'roles') if k in source}
            asset['href'] = f'./{filename}'
            feature['assets'][name] = asset
        part = path.with_name(path.name + '.part')
        try:
            part.write_text(json.dumps({'type': 'FeatureCollection', 'features': list(features.values())}, indent=2))
            part.replace(path)
        finally:
            part.unlink(missing_ok=True)

    def _download_asset(self, href, path):
        r = self._stac_get(href)
        if r.status_code != 302:
            raise GammaEarthError(f'asset not available (HTTP {r.status_code})')
        location = r.headers.get('Location', '')
        if urlsplit(location).scheme != 'https':
            raise GammaEarthError('the API did not give an https download address')
        part = path.with_name(path.name + '.part')
        # The signed address carries its own signature: it is fetched WITHOUT the key, and without compression so the
        # byte count can be compared with Content-Length.
        got = self._request('GET', location, stream=True, headers={'Accept-Encoding': 'identity'})
        try:
            if got.status_code != 200:
                raise GammaEarthError(f'download failed (HTTP {got.status_code})')
            try:
                expected = int(got.headers.get('Content-Length') or -1)
            except ValueError:
                expected = -1
            size, head = 0, b''
            try:
                with open(part, 'wb') as f:
                    for chunk in got.iter_content(1 << 20):
                        if len(head) < 4:
                            head = (head + chunk)[:4]
                        f.write(chunk)
                        size += len(chunk)
            except (requests.RequestException, OSError) as e:
                part.unlink(missing_ok=True)
                raise GammaEarthError(f'download of {path.name} was interrupted ({type(e).__name__})') from None
        finally:
            got.close()
        if head not in TIFF_MAGIC or size == 0 or (expected >= 0 and size != expected):
            part.unlink(missing_ok=True)
            raise GammaEarthError(f'download of {path.name} is not a complete TIFF ({size} of {expected} bytes)')
        part.replace(path)
        return path


class Job:
    """A job of the hosted API. Created by Client.submit() or Client.job()."""

    def __init__(self, client, data):
        if not JOB_ID.fullmatch(str(data['job_id'])):
            raise ValueError('not a job id')
        self._client = client
        self.data = data
        self.job_id = str(data['job_id'])
        pid = data.get('pid')
        self.pid = pid if isinstance(pid, str) and PID.fullmatch(pid) else None
        link = data.get('stac_job')
        # Use the link the API gave only if it is on our API; otherwise build it from the job id.
        self.stac_job = link if client._own(link, 'stac/jobs') else f'{client._api}/stac/jobs/{self.job_id}'
        note = data.get('credits_note')
        # What the API says about the credits of this job, for example that it was a free repeat. Text only, cut, key removed.
        self.credits_note = client._scrub(note)[:300] if isinstance(note, str) else None
        item = data.get('stac_item')
        self.stac_item =item if client._own(item, 'stac/items') else (f'{client._api}/stac/items/{self.pid}' if self.pid else None)

    def __repr__(self):
        return f'Job({self.job_id!r}, pid={self.pid!r})'

    def status(self):
        """The job record from the job API (a dict with at least `state`: pending, '<n>%', completed or failed)."""
        c = self._client
        r = c._request('GET', c._jobs_url(f'/{self.job_id}'))
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 500 or r.status_code == 429:
            raise TransientError(f'job status not available (HTTP {r.status_code})')
        if r.status_code != 200 or not isinstance(data, dict):
            raise GammaEarthError(f'job status not available (HTTP {r.status_code})')
        return data

    def collection(self):
        """One look at the job's STAC collection: a pystac ItemCollection (empty while the job runs), or None when the
        API answers 404 (it does so briefly right after completion, and for a job that is not yours)."""
        c = self._client
        r = c._stac_get(self.stac_job)
        if r.status_code == 404:
            return None
        if r.status_code >= 500 or r.status_code == 429:
            raise TransientError(f'collection not available (HTTP {r.status_code})')
        if r.status_code != 200:
            raise GammaEarthError(f'collection not available (HTTP {r.status_code})')
        try:
            return pystac.ItemCollection.from_dict(r.json())
        except Exception as e:
            raise GammaEarthError(f'the collection could not be read: {type(e).__name__}') from None

    def wait(self, timeout=3000, poll=20, sleep=time.sleep, clock=time.monotonic):
        """Poll until the collection holds the product and return it. The first job after a quiet period is slow
        (the endpoint starts from zero), hence the long default. A few transient errors (network, 5xx, 429) are ridden
        out. Raises JobFailed, JobTimeout, or GammaEarthError if the job completed but no product appears."""
        deadline = clock() + timeout
        transient = completed_without_product = 0
        while True:
            try:
                found = self.collection()
                if found is not None and len(found.items) > 0:
                    return found
                state = self.status().get('state')
                transient = 0
            except TransientError:
                transient += 1
                if transient > MAX_TRANSIENT:
                    raise
                state = None
            if state == 'failed':
                raise JobFailed(f'job {self.job_id} failed')
            if state == 'completed':
                completed_without_product += 1
                if completed_without_product > COMPLETED_GRACE:
                    raise GammaEarthError(f'job {self.job_id} is completed but its collection has no product '
                                          '(is the job yours, and was it made by the hosted API?)')
            if clock() + poll > deadline:
                raise JobTimeout(f'job {self.job_id} has no product after {timeout} s; it may still be running')
            sleep(poll)

    def download(self, dest, assets=None, **wait_options):
        """Wait for the product, then download it (see Client.download); returns the paths."""
        return self._client.download(self.wait(**wait_options), dest, assets)
