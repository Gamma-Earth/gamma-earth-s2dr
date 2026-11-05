"""Client 0.2 (ge-loop T19): search, process, the offline collection file, the catalog."""
import json

from pathlib import Path

import pystac
import pytest
import requests

from gamma_earth import Client, GammaEarthError, NeedsConfirmation
from test_client import API, KEY, PID, Response, Session, feature

SEARCH = f'{API}/stac/search'
SCENE_ID = 'S2B_T36UUB_20240615T085603_L2A'


def product_feature():
    f = feature(('ms', 'tci'))
    f['collection'] = 's2dr3'
    f['properties'].update({'grid:code': 'MGRS-36UUB', 's2dr3:aoi_cloud_cover': 12})
    return f


def scene_feature(credits=4, processable=True, **kw):
    f = {'type': 'Feature', 'stac_version': '1.0.0', 'id': SCENE_ID, 'collection': 'sentinel-2-c1-l2a', 'bbox': [30, 51, 31, 52],
         'geometry': {'type': 'Polygon', 'coordinates': [[[30, 51], [31, 51], [31, 52], [30, 52], [30, 51]]]},
         'properties': {'datetime': '2024-06-15T08:56:03Z', 'eo:cloud_cover': 45.1, 's2dr3:aoi_cloud_cover': 14, 's2dr3:status': 'unprocessed',
                        's2dr3:processable': processable, 'grid:code': 'MGRS-36UUB', 's2dr3:area_km2': 7.7},
         'assets': {'thumbnail': {'href': 'https://e84.s3.us-west-2.amazonaws.com/t.jpg', 'roles': ['thumbnail']}},
         'links': [{'rel': 'via', 'href': 'https://earth-search.aws.element84.com/v1/items/x'}]}
    if processable:
        f['links'].append({'rel': 's2dr3:process', 'href': f'{API}/jobs/{{api_key}}', 'method': 'POST', 'templated': True,
                           'body': {'aoi': '30.9060000 51.3940000 30.9360000 51.4220000', 'date': '2024-06-15', 'date_range': 0, 'max_clouds': 100}})
        if credits is not None:
            f['properties']['s2dr3:estimated_credits'] = credits
    f.update(kw)
    return f


def page(features, next_body=None, **extra):
    links = [{'rel': 'self', 'href': SEARCH}, {'rel': 'root', 'href': f'{API}/stac'}]
    if next_body is not None:
        links.append({'rel': 'next', 'href': SEARCH, 'method': 'POST', 'body': next_body})
    return {'type': 'FeatureCollection', 'features': features, 'numberReturned': len(features), 'links': links, **extra}


def client(routes):
    s = Session(routes)
    return Client(API, KEY, session=s), s


def sent(session, n=0):
    return [kw for m, u, kw in session.calls if m == 'POST' and u == SEARCH][n]


# --- search --------------------------------------------------------------------------------------------------------------------

def test_a_search_posts_the_filters_with_the_key_in_the_header_never_in_the_url():
    c, s = client({('POST', SEARCH): Response(200, page([product_feature(), scene_feature()]))})
    result = c.search(bbox=(30.9, 51.3, 31.0, 51.5), datetime='2024-06-01/2024-06-30', max_clouds=20, collections=['s2dr3'], limit=5)
    kw = sent(s)
    assert kw['json'] == {'bbox': [30.9, 51.3, 31.0, 51.5], 'datetime': '2024-06-01/2024-06-30', 'max_clouds': 20.0, 'collections': ['s2dr3'], 'limit': 5}
    assert kw['headers'] == {'X-API-Key': KEY} and kw['allow_redirects'] is False
    assert all(KEY not in u for _, u, _ in s.calls)
    assert [i.id for i in result] == [PID, SCENE_ID] and len(result) == 2


def test_the_result_separates_products_and_scenes_and_carries_the_warnings():
    body = page([product_feature(), scene_feature()], warnings=['x'], **{'s2dr3:scenes': {'returned': 1, 'looked_at': 20}})
    c, _ = client({('POST', SEARCH): Response(200, body)})
    r = c.search(bbox=(30.9, 51.3, 31.0, 51.5))
    assert [i.id for i in r.products] == [PID] and [i.id for i in r.scenes] == [SCENE_ID]
    assert r.warnings == ['x'] and r.scenes_info == {'returned': 1, 'looked_at': 20}
    assert all(isinstance(i, pystac.Item) for i in r)


def test_no_filter_is_sent_for_what_was_not_asked():
    c, s = client({('POST', SEARCH): Response(200, page([]))})
    c.search()
    assert sent(s)['json'] == {'limit': 10}


def test_a_tile_and_extra_query_are_merged_into_the_query():
    c, s = client({('POST', SEARCH): Response(200, page([]))})
    c.search(tile='36UUB', query={'s2dr3:aoi_cloud_cover': {'gte': 5}})
    assert sent(s)['json']['query'] == {'grid:code': {'eq': 'MGRS-36UUB'}, 's2dr3:aoi_cloud_cover': {'gte': 5}}


def test_a_geometry_can_be_searched_for():
    point = {'type': 'Point', 'coordinates': [30.9, 51.4]}
    c, s = client({('POST', SEARCH): Response(200, page([]))})
    c.search(intersects=point)
    assert sent(s)['json']['intersects'] == point


def test_dates_can_be_given_as_dates_or_a_pair():
    import datetime as dt
    c, s = client({('POST', SEARCH): Response(200, page([])), })
    c.search(datetime=(dt.date(2024, 6, 1), dt.date(2024, 6, 30)))
    assert sent(s)['json']['datetime'] == '2024-06-01/2024-06-30'
    c.search(datetime=dt.date(2024, 6, 13))
    assert sent(s, 1)['json']['datetime'] == '2024-06-13'
    c.search(datetime=(None, dt.date(2024, 6, 30)))
    assert sent(s, 2)['json']['datetime'] == '../2024-06-30'


@pytest.mark.parametrize('bad', [{'limit': 0}, {'limit': 101}, {'limit': True}, {'max_clouds': -1}, {'max_clouds': 101}, {'max_clouds': 'x'},
                                 {'bbox': (1, 2, 3)}, {'bbox': 'x'}, {'ids': 'T36UUB-a-20240613'}, {'tile': 'x'}, {'tile': '36uub'},
                                 {'collections': 'x'}, {'datetime': 5}, {'datetime': (1, 2)}, {'bbox': (1, 2, 3, 4), 'intersects': {'type': 'Point', 'coordinates': [1, 2]}},
                                 {'max_items': 0}, {'max_items': -3}])
def test_a_bad_argument_is_refused_before_anything_is_sent(bad):
    c, s = client({})
    with pytest.raises((ValueError, TypeError)):
        c.search(**bad)
    assert s.calls == []


@pytest.mark.parametrize('status, text', [(400, 'unknown parameter max_cloud.'), (401, 'Invalid API key.'), (503, 'Service temporarily unavailable.')])
def test_an_error_from_the_api_is_an_error_with_its_text_and_no_key(status, text):
    c, _ = client({('POST', SEARCH): Response(status, {'error': f'{text} {KEY}'})})
    with pytest.raises(GammaEarthError) as e:
        c.search()
    assert str(status) in str(e.value) and KEY not in str(e.value)


def test_a_503_is_retried_as_a_transient_error_only_by_the_caller():
    from gamma_earth.client import TransientError
    c, _ = client({('POST', SEARCH): Response(503, {})})
    with pytest.raises(TransientError):
        c.search()


def test_a_body_that_is_not_a_feature_collection_is_an_error():
    for body in ({}, [], {'type': 'Feature'}, {'type': 'FeatureCollection', 'features': 5}, {'type': 'FeatureCollection', 'features': [5]}):
        c, _ = client({('POST', SEARCH): Response(200, body)})
        with pytest.raises(GammaEarthError):
            c.search()


# --- paging ---------------------------------------------------------------------------------------------------------------------

def test_max_items_follows_the_next_links_of_the_products_with_the_same_key_and_stops_at_the_cap():
    a, b, c3 = product_feature(), product_feature(), product_feature()
    a['id'], b['id'], c3['id'] = 'T18NWL-a00000000-20240101', 'T18NWL-b00000000-20240102', 'T18NWL-c00000000-20240103'
    first = page([a, scene_feature()], next_body={'limit': 1, 'token': 't1'})
    second = page([b], next_body={'limit': 1, 'token': 't2'})
    third = page([c3])
    c, s = client({('POST', SEARCH): [Response(200, first), Response(200, second), Response(200, third)]})
    r = c.search(limit=1, max_items=2)
    assert [i.id for i in r.products] == [a['id'], b['id']] and [i.id for i in r.scenes] == [SCENE_ID]
    assert len(s.calls) == 2 and sent(s, 1)['json'] == {'limit': 1, 'token': 't1'}


def test_without_max_items_only_the_first_page_is_read():
    c, s = client({('POST', SEARCH): Response(200, page([product_feature()], next_body={'token': 't'}))})
    c.search()
    assert len(s.calls) == 1


def test_a_next_link_to_another_host_is_never_followed():
    f = page([product_feature()])
    f['links'].append({'rel': 'next', 'href': 'https://evil.example/stac/search', 'method': 'POST', 'body': {'token': 't'}})
    c, s = client({('POST', SEARCH): Response(200, f)})
    r = c.search(max_items=50)
    assert len(r.products) == 1 and all('evil' not in u for _, u, _ in s.calls)


def test_a_next_link_with_a_bad_body_ends_the_paging():
    for nxt in ({'rel': 'next', 'href': SEARCH, 'method': 'POST'}, {'rel': 'next', 'href': SEARCH, 'method': 'POST', 'body': 'x'},
                {'rel': 'next', 'href': SEARCH, 'method': 'GET'}):
        f = page([product_feature()])
        f['links'].append(nxt)
        c, s = client({('POST', SEARCH): Response(200, f)})
        assert len(c.search(max_items=50).products) == 1 and len(s.calls) == 1


def test_paging_stops_at_a_page_cap_even_if_the_api_never_ends():
    forever = page([product_feature()], next_body={'token': 't'})
    c, s = client({('POST', SEARCH): Response(200, forever)})
    c.search(limit=1, max_items=10 ** 6)
    assert len(s.calls) <= 20


# --- process a scene --------------------------------------------------------------------------------------------------------------------

JOBS = f'{API}/jobs/{KEY}'
CREATED = {'job_id': 'j1', 'pid': PID, 'stac_job': f'{API}/stac/jobs/j1', 'stac_item': f'{API}/stac/items/{PID}'}


def scene_item(**kw):
    return pystac.Item.from_dict(scene_feature(**kw))


def test_processing_needs_a_confirmation_that_names_the_price_and_sends_nothing_without_it():
    c, s = client({('POST', JOBS): Response(200, CREATED)})
    with pytest.raises(NeedsConfirmation) as e:
        c.process(scene_item())
    assert e.value.credits == 4 and '4 credits' in str(e.value) and s.calls == []


def test_a_confirmed_process_submits_the_job_the_link_describes():
    c, s = client({('POST', JOBS): Response(200, CREATED)})
    job = c.process(scene_item(), confirm=True)
    assert job.job_id == 'j1'
    ((_, url, kw),) = s.calls
    assert url == JOBS and kw['json'] == {'date': '2024-06-15', 'aoi': '30.9060000 51.3940000 30.9360000 51.4220000', 'date_range': 0, 'max_clouds': 100}


def test_a_confirmation_can_be_limited_to_a_price():
    c, s = client({('POST', JOBS): Response(200, CREATED)})
    with pytest.raises(NeedsConfirmation):
        c.process(scene_item(credits=9), confirm=True, max_credits=5)
    assert s.calls == []
    assert c.process(scene_item(credits=4), confirm=True, max_credits=5).job_id == 'j1'


def test_a_scene_with_no_known_price_needs_a_confirmation_that_says_so_and_a_max_credits_is_then_refused():
    c, s = client({('POST', JOBS): Response(200, CREATED)})
    with pytest.raises(NeedsConfirmation) as e:
        c.process(scene_item(credits=None))
    assert e.value.credits is None and 'no price' in str(e.value)
    with pytest.raises(NeedsConfirmation):
        c.process(scene_item(credits=None), confirm=True, max_credits=100)
    assert s.calls == []


def test_a_product_cannot_be_processed():
    c, s = client({})
    with pytest.raises(GammaEarthError):
        c.process(pystac.Item.from_dict(product_feature()), confirm=True)
    assert s.calls == []


def test_a_scene_too_large_for_a_job_cannot_be_processed():
    c, s = client({})
    with pytest.raises(GammaEarthError):
        c.process(scene_item(processable=False), confirm=True)
    assert s.calls == []


@pytest.mark.parametrize('change', [
    lambda l: l.update(href='https://evil.example/jobs/{api_key}'),
    lambda l: l.update(href=f'{API}/jobs/'),
    lambda l: l.update(href=f'{API}/other/{{api_key}}'),
    lambda l: l.update(body={}),
    lambda l: l.update(body='x'),
    lambda l: l['body'].update(extra='x'),
    lambda l: l['body'].update(aoi='x'),
    lambda l: l['body'].update(aoi='1 2 3'),
    lambda l: l['body'].update(date='yesterday'),
    lambda l: l['body'].update(date_range='0'),
    lambda l: l['body'].update(max_clouds=500),
    lambda l: l.update(method='GET')])
def test_a_process_link_that_is_not_what_the_api_gives_is_refused_and_nothing_is_sent(change):
    f = scene_feature()
    link = next(l for l in f['links'] if l['rel'] == 's2dr3:process')
    change(link)
    c, s = client({('POST', JOBS): Response(200, CREATED)})
    with pytest.raises(GammaEarthError):
        c.process(pystac.Item.from_dict(f), confirm=True)
    assert s.calls == []


def test_the_key_is_never_in_the_process_error():
    f = scene_feature()
    next(l for l in f['links'] if l['rel'] == 's2dr3:process')['body']['aoi'] = KEY
    c, _ = client({})
    with pytest.raises(GammaEarthError) as e:
        c.process(pystac.Item.from_dict(f), confirm=True)
    assert KEY not in str(e.value)


def test_a_scene_can_be_given_as_a_plain_dict_too():
    c, s = client({('POST', JOBS): Response(200, CREATED)})
    assert c.process(scene_feature(), confirm=True).job_id == 'j1'


# --- the offline collection file ------------------------------------------------------------------------------------------------------

from test_client import CF, TIFF, asset_route, items as one_item_collection  # noqa: E402


def test_a_download_writes_a_collection_file_that_pystac_reads_offline(tmp_path):
    c, _ = client(asset_route())
    (path,) = c.download(one_item_collection('tci'), tmp_path, assets=['tci'])
    listing = tmp_path / 'collection.json'
    loaded = pystac.ItemCollection.from_file(str(listing))
    (item,) = list(loaded)
    assert item.id == PID and list(item.assets) == ['tci'] and item.assets['tci'].href == f'./{PID}_tci.tif'
    assert (tmp_path / f'{PID}_tci.tif').read_bytes() == TIFF and path.name == f'{PID}_tci.tif'
    assert item.links == []


def test_the_collection_file_names_only_the_assets_that_were_downloaded(tmp_path):
    routes = {**asset_route('tci'), **asset_route('ms', CF.replace('TCI', 'MS'))}
    c, _ = client(routes)
    c.download(one_item_collection('ms', 'tci'), tmp_path, assets=['tci'])
    (item,) = list(pystac.ItemCollection.from_file(str(tmp_path / 'collection.json')))
    assert list(item.assets) == ['tci']


def test_the_collection_file_holds_no_key_and_no_api_address(tmp_path):
    c, _ = client(asset_route())
    c.download(one_item_collection('tci'), tmp_path)
    text = (tmp_path / 'collection.json').read_text()
    assert KEY not in text and API not in text and 'execute-api' not in text and 'cloudfront' not in text


def test_a_failed_download_leaves_an_earlier_collection_file_alone_and_no_temporary_file(tmp_path):
    (tmp_path / 'collection.json').write_text('{"old": true}')
    routes = asset_route()
    routes[('GET', CF)] = Response(200, headers={'Content-Length': '999'}, content=TIFF)
    c, _ = client(routes)
    with pytest.raises(GammaEarthError):
        c.download(one_item_collection('tci'), tmp_path)
    assert (tmp_path / 'collection.json').read_text() == '{"old": true}'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['collection.json']


def test_job_download_writes_the_collection_file_too(tmp_path):
    from test_client import make_job, STAC, collection
    job, _ = make_job({('GET', STAC): Response(200, collection(feature(('tci',)))), **asset_route()})
    job.download(tmp_path, assets=['tci'])
    assert (tmp_path / 'collection.json').exists()


# --- the catalog ----------------------------------------------------------------------------------------------------------------------------------

def catalog_routes():
    root = {'type': 'Catalog', 'stac_version': '1.0.0', 'id': 's2dr3-stac', 'description': 'x',
            'links': [{'rel': 'self', 'href': f'{API}/stac'}, {'rel': 'root', 'href': f'{API}/stac'},
                      {'rel': 'child', 'href': f'{API}/stac/collections/s2dr3', 'type': 'application/json'}]}
    col = {'type': 'Collection', 'stac_version': '1.0.0', 'id': 's2dr3', 'description': 'x', 'license': 'proprietary',
           'extent': {'spatial': {'bbox': [[-180, -90, 180, 90]]}, 'temporal': {'interval': [['2015-06-27T00:00:00Z', None]]}},
           'links': [{'rel': 'self', 'href': f'{API}/stac/collections/s2dr3'}, {'rel': 'root', 'href': f'{API}/stac'}]}
    return {('GET', f'{API}/stac'): Response(200, root), ('GET', f'{API}/stac/collections/s2dr3'): Response(200, col)}


def test_the_catalog_lists_the_collection_through_pystac_with_the_key_in_the_header_only():
    c, s = client(catalog_routes())
    cat = c.catalog()
    assert [x.id for x in cat.get_collections()] == ['s2dr3']
    assert s.calls and all(kw['headers'] == {'X-API-Key': KEY} and kw['allow_redirects'] is False and KEY not in u for _, u, kw in s.calls)


def test_the_catalog_will_not_read_from_another_host_and_will_not_send_the_key_there():
    c, s = client({})
    io = c.stac_io()
    with pytest.raises(GammaEarthError):
        io.read_text('https://evil.example/stac')
    assert s.calls == []


def test_the_catalog_io_refuses_to_write():
    c, _ = client({})
    with pytest.raises(GammaEarthError):
        c.stac_io().write_text('x', 'y')


def test_the_catalog_io_reports_an_error_status_without_the_key():
    c, _ = client({('GET', f'{API}/stac'): Response(401, {'error': f'Invalid {KEY}'})})
    with pytest.raises(GammaEarthError) as e:
        c.stac_io().read_text(f'{API}/stac')
    assert '401' in str(e.value) and KEY not in str(e.value)



# --- gaps found by the mutation check ---------------------------------------------------------------------------------------------------------------

def process_link(f):
    return next(l for l in f['links'] if l['rel'] == 's2dr3:process')


@pytest.mark.parametrize('change', [
    lambda f: f.update(collection='s2dr3'),
    lambda f: f['properties'].update({'s2dr3:status': 'processed'}),
    lambda f: f['properties'].update({'s2dr3:processable': False}),
    lambda f: process_link(f)['body'].update(aoi='a b c d'),
    lambda f: process_link(f)['body'].update(date_range=100),
    lambda f: process_link(f)['body'].update(date_range=-1),
    lambda f: process_link(f)['body'].update(date_range=True),
    lambda f: f['links'].append(dict(process_link(f)))])
def test_a_scene_that_is_not_plainly_processable_is_refused_even_with_a_process_link(change):
    f = scene_feature()
    change(f)
    c, s = client({('POST', JOBS): Response(200, CREATED)})
    with pytest.raises(GammaEarthError):
        c.process(pystac.Item.from_dict(f), confirm=True)
    assert s.calls == []


def test_max_items_cuts_the_products_of_a_page_that_has_more():
    many = []
    for n in range(3):
        f = product_feature()
        f['id'] = f'T18NWL-a0000000{n}-2024010{n + 1}'
        many.append(f)
    c, _ = client({('POST', SEARCH): Response(200, page(many))})
    assert len(c.search(limit=3, max_items=2).products) == 2


def test_a_huge_next_body_is_not_followed():
    c, s = client({('POST', SEARCH): Response(200, page([product_feature()], next_body={'token': 'x' * 100000}))})
    assert len(c.search(max_items=50).products) == 1 and len(s.calls) == 1


def test_a_feature_without_a_features_list_is_an_error():
    c, _ = client({('POST', SEARCH): Response(200, {'type': 'Feature', 'features': []})})
    with pytest.raises(GammaEarthError):
        c.search()


def test_the_collection_file_has_no_links_and_no_collection_name(tmp_path):
    f = feature(('tci',))
    f['links'] = [{'rel': 'self', 'href': f'{API}/stac/items/{PID}'}, {'rel': 'collection', 'href': f'{API}/stac/collections/s2dr3'}]
    f['collection'] = 's2dr3'
    c, _ = client(asset_route())
    c.download(pystac.ItemCollection([pystac.Item.from_dict(f)]), tmp_path)
    (written,) = json.loads((tmp_path / 'collection.json').read_text())['features']
    assert written['links'] == [] and 'collection' not in written


def test_a_collection_file_that_cannot_be_finished_leaves_the_earlier_one_alone(tmp_path, monkeypatch):
    (tmp_path / 'collection.json').write_text('{"old": true}')
    real = Path.write_text

    def broken(self, text, *a, **k):
        real(self, text[:5], *a, **k)
        raise OSError('disk full')
    c, _ = client(asset_route())
    monkeypatch.setattr(Path, 'write_text', broken)
    with pytest.raises(OSError):
        c.download(one_item_collection('tci'), tmp_path)
    monkeypatch.undo()
    assert (tmp_path / 'collection.json').read_text() == '{"old": true}'
    assert not (tmp_path / 'collection.json.part').exists()


# --- findings of the code review ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('bad', [{'bbox': (float('nan'), 51, 31, 52)}, {'bbox': (30, 51, float('inf'), 52)},
                                 {'tile': '36UUB', 'query': {'grid:code': {'eq': 'MGRS-35UPB'}}}, {'query': ['x']}])
def test_more_bad_arguments_are_refused_before_anything_is_sent(bad):
    c, s = client({('POST', SEARCH): Response(200, page([]))})
    with pytest.raises(ValueError):
        c.search(**bad)
    assert s.calls == []


def test_a_search_cut_by_the_page_cap_says_so():
    forever = page([product_feature()], next_body={'token': 't'})
    c, _ = client({('POST', SEARCH): Response(200, forever)})
    assert any('stopped after' in w for w in c.search(limit=1, max_items=10 ** 6).warnings)
    c, _ = client({('POST', SEARCH): Response(200, page([product_feature()]))})
    assert c.search(max_items=5).warnings == []


def test_a_fractional_price_is_rounded_up_for_the_limit():
    c, s = client({('POST', JOBS): Response(200, CREATED)})
    with pytest.raises(NeedsConfirmation) as e:
        c.process(scene_item(credits=4.2), confirm=True, max_credits=4)
    assert e.value.credits == 5 and s.calls == []


def test_a_second_download_into_the_same_folder_keeps_the_first_in_the_listing(tmp_path):
    first = feature(('tci',))
    routes = asset_route()
    c, _ = client(routes)
    c.download(pystac.ItemCollection([pystac.Item.from_dict(first)]), tmp_path)
    c.download(pystac.ItemCollection([pystac.Item.from_dict(first)]), tmp_path)
    old = json.loads((tmp_path / 'collection.json').read_text())
    old['features'].append({'type': 'Feature', 'id': 'earlier', 'assets': {}, 'links': [], 'properties': {}})
    (tmp_path / 'collection.json').write_text(json.dumps(old))
    c.download(pystac.ItemCollection([pystac.Item.from_dict(first)]), tmp_path)
    ids = [f['id'] for f in json.loads((tmp_path / 'collection.json').read_text())['features']]
    assert sorted(ids) == sorted(['earlier', PID])


def test_nothing_downloaded_leaves_the_collection_file_alone(tmp_path):
    (tmp_path / 'collection.json').write_text('{"old": true}')
    c, _ = client({})
    c.download(pystac.ItemCollection([]), tmp_path)
    assert (tmp_path / 'collection.json').read_text() == '{"old": true}'


def test_the_collection_file_keeps_no_asset_field_that_could_hold_an_address(tmp_path):
    f = feature(('tci',))
    f['assets']['tci'].update({'alternate': {'s3': {'href': 's3://bucket/secret'}}, 'storage:path': 's3://bucket/secret'})
    c, _ = client(asset_route())
    c.download(pystac.ItemCollection([pystac.Item.from_dict(f)]), tmp_path)
    assert 's3://' not in (tmp_path / 'collection.json').read_text()

