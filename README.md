# gamma-earth-s2dr

Python client for the Gamma Earth S2DR3 hosted API. It submits a job, follows the job's STAC collection and downloads
the products. It is **not** the Colab SDK (`ge-sdk`, which runs the model on your own GPU): this talks to the hosted API
with your API key.

> Status: 0.2 development version, tested against stage only. Nothing here is published to PyPI.

```python
from gamma_earth_s2dr import Client

client = Client()                       # GE_API_URL and GE_API_KEY from the environment, or Client(api_url, api_key)
job = client.submit((30.906, 51.394, 30.936, 51.422), "2024-06-15")   # lon_min lat_min lon_max lat_max; spends credits
files = job.download("out/")            # waits for the product (minutes; the first job after a quiet spell is slower)
```

Step by step: `job.wait()` returns a `pystac.ItemCollection` (empty while the job runs); `client.download(collection, "out/",
assets=["tci"])` fetches chosen assets (names: `ms`, `tci`, `irp`, `ndvi`, and `*_10m` for the original-resolution files;
older products offer only `ms` and `tci`). To follow a job you already created: `client.job("<job id>")`.

`submit` spends credits and is **not** safe to retry: if it fails after the request was sent (a timeout, a lost answer) the
job may exist, so check your jobs before submitting again. `wait` rides out a few transient errors (network, 5xx, 429),
stops with `JobFailed` if the job failed, and says so if a job is `completed` but no product ever appears.

## Search (0.2)

```python
client = Client()
found = client.search(bbox=(30.9, 51.39, 30.94, 51.42), datetime=('2024-06-01', '2024-06-30'), max_clouds=20)
found.products      # your finished products (collection s2dr3)
found.scenes        # unprocessed Sentinel-2 scenes over that area, with a price shown
found.warnings

client.search(ids=['T36UUB-...'])            # by product id
client.search(tile='36UUB', max_items=200)   # follow the next pages (at most 20)

job = client.process(found.scenes[0])                       # NeedsConfirmation: shows the price, spends nothing
job = client.process(found.scenes[0], confirm=True, max_credits=10)
job.download('out/')                          # also writes out/collection.json
```
A product you already paid for is free to make again, and a search does not list scenes for an area and day you already
have a product for. If a job turns out to be a free repeat, `job.credits_note` says so.

`client.catalog()` gives the API as a pystac `Catalog` (browse the collection `s2dr3`); searching is `client.search()`, which
returns pystac Items. After a download, `pystac.ItemCollection.from_file('out/collection.json')`
works offline: it lists only the files you downloaded, by relative path, without the key or the API address.

Every job created through this package carries `"client": "gamma-earth-s2dr/<version>"`, so the API records it as
`S2DR3P` (P = Python package) in the usage report; plain API calls stay `S2DR3A`.

## Keys and safety

- The key goes only to your API host: in the URL path for the job API (that is how the job API works today), in the
  `X-API-Key` header for the STAC API. It is never sent to the signed download address, never printed, and is removed from
  every error message. A link in an API answer that is not on the API host is ignored.
- Downloads are written to `<name>.part` and renamed only when the size equals Content-Length and the file is a TIFF.
- There is no default API URL: prod has no STAC API yet.

## Develop

```
pip install -e ".[test]"
python -m pytest -q
```
