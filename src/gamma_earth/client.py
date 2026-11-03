"""Client for the Gamma Earth S2DR3 hosted API: submit a job, follow its STAC collection, download the products.

The flow is the one proven on stage (ticket T14): POST a job, poll the `stac_job` link it returns until the
collection holds the item, then fetch each asset through the API's redirect to a short-lived signed URL.

The API key is sent only to the API host (in the URL path for the job API, in the X-API-Key header for the STAC API)
and never to the signed download host. It is never printed or logged, is scrubbed from every error text, and no request
that carries it follows a redirect.
"""
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import pystac
import requests

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


class Client:
    """`Client(api_url, api_key)`. Both default to the environment variables GE_API_URL and GE_API_KEY."""

    def __init__(self, api_url=None, api_key=None, session=None):
        api_url = (api_url or os.environ.get('GE_API_URL') or '').rstrip('/')
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
                plan.append((href, f'{item.id}_{name}.tif'))
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        return [self._download_asset(href, dest / filename) for href, filename in plan]

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
        item = data.get('stac_item')
        self.stac_item = item if client._own(item, 'stac/items') else (f'{client._api}/stac/items/{self.pid}' if self.pid else None)

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
