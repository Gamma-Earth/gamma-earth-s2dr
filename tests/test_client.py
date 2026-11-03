import pytest
import requests

from gamma_earth import Client, GammaEarthError, JobFailed, JobTimeout

API = 'https://abc123.execute-api.eu-central-1.amazonaws.com/stage'
KEY = 'k' * 32
CF = 'https://d111.cloudfront.net/UA/T36UUB/P/S2L2Ax10_P_TCI.tif?Expires=1&Signature=s&Key-Pair-Id=K'
PID = 'T36UUB-945f7dd92-20240613'
TIFF = b'II*\x00' + b'x' * 100


class Response:
    def __init__(self, status=200, body=None, headers=None, content=b''):
        self.status_code, self._body, self.headers, self._content = status, body, headers or {}, content

    def json(self):
        if self._body is None:
            raise ValueError('no json')
        return self._body

    def iter_content(self, size):
        yield self._content

    def close(self):
        pass


class Session:
    """Answers by (method, url); records every request so tests can see where the key went."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        answer = self.routes[(method, url)]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def feature(assets=('ms', 'tci')):
    return {'type': 'Feature', 'stac_version': '1.0.0', 'id': PID, 'bbox': [30, 51, 31, 52], 'links': [],
            'geometry': {'type': 'Polygon', 'coordinates': [[[30, 51], [31, 51], [31, 52], [30, 52], [30, 51]]]},
            'properties': {'datetime': '2024-06-13T00:00:00Z'},
            'assets': {n: {'href': f'{API}/stac/assets/{PID}/{n}'} for n in assets}}


def collection(*features):
    return {'type': 'FeatureCollection', 'features': list(features), 'links': []}


CREATED = {'job_id': 'j1', 'pid': PID, 'stac_job': f'{API}/stac/jobs/j1', 'stac_item': f'{API}/stac/items/{PID}'}


def client(routes):
    s = Session(routes)
    return Client(API, KEY, session=s), s


# --- the client ---------------------------------------------------------------------------------------------------

def test_the_api_url_must_be_https_and_the_key_is_required(monkeypatch):
    monkeypatch.delenv('GE_API_URL', raising=False)
    monkeypatch.delenv('GE_API_KEY', raising=False)
    with pytest.raises(ValueError):
        Client('http://x.example', KEY)
    with pytest.raises(ValueError):
        Client(API, '')
    with pytest.raises(ValueError):
        Client()


def test_the_environment_is_used_when_nothing_is_passed(monkeypatch):
    monkeypatch.setenv('GE_API_URL', API + '/')
    monkeypatch.setenv('GE_API_KEY', KEY)
    assert Client()._api == API


def test_the_key_is_not_in_the_repr():
    assert KEY not in repr(Client(API, KEY))


# --- submit -------------------------------------------------------------------------------------------------------

def test_submit_posts_the_job_and_returns_its_links():
    c, s = client({('POST', f'{API}/jobs/{KEY}'): Response(200, CREATED)})
    job = c.submit((30.906, 51.394, 30.936, 51.422), '2024-06-15')
    (_, _, kw), = s.calls
    assert kw['json'] == {'date': '2024-06-15', 'aoi': '30.906 51.394 30.936 51.422', 'date_range': 7, 'max_clouds': 30}
    assert (job.job_id, job.pid, job.stac_job) == ('j1', PID, f'{API}/stac/jobs/j1')


def test_an_aoi_string_is_accepted_and_a_bad_box_is_refused():
    c, s = client({('POST', f'{API}/jobs/{KEY}'): Response(200, CREATED)})
    c.submit('30.9  51.3 30.9 51.4', '2024-06-15')
    assert s.calls[0][2]['json']['aoi'] == '30.9 51.3 30.9 51.4'
    with pytest.raises(ValueError):
        c.submit((1, 2, 3), '2024-06-15')


def test_a_link_that_is_not_on_our_api_is_replaced_by_one_built_from_the_job_id():
    evil = dict(CREATED, stac_job='https://evil.example/stac/jobs/j1', stac_item='https://evil.example/x')
    c, _ = client({('POST', f'{API}/jobs/{KEY}'): Response(200, evil)})
    job = c.submit('1 2 3 4', '2024-06-15')
    assert job.stac_job == f'{API}/stac/jobs/j1' and job.stac_item == f'{API}/stac/items/{PID}'


def test_a_refused_job_raises_and_the_error_has_no_key():
    c, _ = client({('POST', f'{API}/jobs/{KEY}'): Response(402, {'error': f'not enough credits for {KEY}'})})
    with pytest.raises(GammaEarthError) as e:
        c.submit('1 2 3 4', '2024-06-15')
    assert KEY not in str(e.value) and '402' in str(e.value)


def test_a_non_200_answer_is_a_failure_even_if_the_body_looks_like_a_job():
    c, _ = client({('POST', f'{API}/jobs/{KEY}'): Response(500, CREATED)})
    with pytest.raises(GammaEarthError):
        c.submit('1 2 3 4', '2024-06-15')


def test_a_network_error_has_no_key_in_it():
    c, _ = client({('POST', f'{API}/jobs/{KEY}'): requests.ConnectionError(f'failed to reach {API}/jobs/{KEY}')})
    with pytest.raises(GammaEarthError) as e:
        c.submit('1 2 3 4', '2024-06-15')
    assert KEY not in str(e.value)


# --- wait ---------------------------------------------------------------------------------------------------------

def make_job(routes):
    c, s = client({('POST', f'{API}/jobs/{KEY}'): Response(200, CREATED), **routes})
    return c.submit('1 2 3 4', '2024-06-15'), s


STAC = f'{API}/stac/jobs/j1'
STATUS = f'{API}/jobs/{KEY}/j1'


def test_wait_polls_through_empty_and_404_answers_and_returns_the_collection():
    job, s = make_job({
        ('GET', STAC): [Response(200, collection()), Response(404, {'error': 'Not found.'}), Response(200, collection(feature()))],
        ('GET', STATUS): Response(200, {'state': '50%'})})
    sleeps = []
    found = job.wait(sleep=sleeps.append, poll=5)
    assert [i.id for i in found] == [PID] and sleeps == [5, 5]
    assert all(kw['headers'] == {'X-API-Key': KEY} for m, u, kw in s.calls if u == STAC)


def test_wait_stops_when_the_job_failed():
    job, _ = make_job({('GET', STAC): Response(200, collection()), ('GET', STATUS): Response(200, {'state': 'failed'})})
    now = [0.0]          # a fake clock: if the failure were missed, this would time out instead of hanging
    with pytest.raises(JobFailed):
        job.wait(timeout=60, poll=20, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])


def test_wait_times_out():
    job, _ = make_job({('GET', STAC): Response(200, collection()), ('GET', STATUS): Response(200, {'state': '10%'})})
    now = [0.0]
    with pytest.raises(JobTimeout):
        job.wait(timeout=60, poll=20, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])


def test_wait_raises_on_other_http_errors():
    job, _ = make_job({('GET', STAC): Response(500, {})})
    with pytest.raises(GammaEarthError):
        job.wait(sleep=lambda s: None)


# --- download -----------------------------------------------------------------------------------------------------

def asset_route(name='tci', location=CF):
    return {('GET', f'{API}/stac/assets/{PID}/{name}'): Response(302, headers={'Location': location}),
            ('GET', location): Response(200, headers={'Content-Length': str(len(TIFF))}, content=TIFF)}


def items(*names):
    import pystac
    return pystac.ItemCollection.from_dict(collection(feature(names or ('tci',))))


def test_download_sends_the_key_to_the_api_and_never_to_the_signed_address(tmp_path):
    c, s = client(asset_route())
    (path,) = c.download(items('tci'), tmp_path, assets=['tci'])
    assert path.name == f'{PID}_tci.tif' and path.read_bytes() == TIFF
    api_call = next(kw for m, u, kw in s.calls if u.startswith(API))
    cf_call = next(kw for m, u, kw in s.calls if u == CF)
    assert api_call['headers'] == {'X-API-Key': KEY} and api_call['allow_redirects'] is False
    assert 'headers' not in cf_call and KEY not in repr(cf_call)
    assert not list(tmp_path.glob('*.part'))


def test_download_takes_every_asset_by_default(tmp_path):
    routes = {**asset_route('tci'), **asset_route('ms', CF.replace('TCI', 'MS'))}
    c, _ = client(routes)
    names = sorted(p.name for p in c.download(items('ms', 'tci'), tmp_path))
    assert names == [f'{PID}_ms.tif', f'{PID}_tci.tif']


def test_an_unknown_asset_name_is_refused(tmp_path):
    c, _ = client({})
    with pytest.raises(GammaEarthError):
        c.download(items('tci'), tmp_path, assets=['irp'])


def test_an_asset_link_off_our_api_is_refused_and_nothing_is_sent(tmp_path):
    import pystac
    doc = collection(feature(('tci',)))
    doc['features'][0]['assets']['tci']['href'] = 'https://evil.example/steal'
    c, s = client({})
    with pytest.raises(GammaEarthError):
        c.download(pystac.ItemCollection.from_dict(doc), tmp_path)
    assert s.calls == []


def test_an_asset_that_is_not_available_raises(tmp_path):
    c, _ = client({('GET', f'{API}/stac/assets/{PID}/tci'): Response(404, {'error': 'Not found.'})})
    with pytest.raises(GammaEarthError):
        c.download(items('tci'), tmp_path)


def test_a_truncated_download_leaves_no_file(tmp_path):
    routes = asset_route()
    routes[('GET', CF)] = Response(200, headers={'Content-Length': str(len(TIFF) + 10)}, content=TIFF)
    c, _ = client(routes)
    with pytest.raises(GammaEarthError):
        c.download(items('tci'), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_file_that_is_not_a_tiff_leaves_no_file(tmp_path):
    routes = asset_route()
    routes[('GET', CF)] = Response(200, headers={'Content-Length': '5'}, content=b'<html')
    c, _ = client(routes)
    with pytest.raises(GammaEarthError):
        c.download(items('tci'), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_redirect_that_is_not_https_is_refused(tmp_path):
    c, _ = client({('GET', f'{API}/stac/assets/{PID}/tci'): Response(302, headers={'Location': 'http://x.example/f.tif'})})
    with pytest.raises(GammaEarthError):
        c.download(items('tci'), tmp_path)


def test_job_download_waits_then_downloads(tmp_path):
    job, _ = make_job({('GET', STAC): Response(200, collection(feature(('tci',)))), **asset_route()})
    (path,) = job.download(tmp_path, assets=['tci'])
    assert path.read_bytes() == TIFF
