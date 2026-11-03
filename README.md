# gamma-earth-client

Python client for the Gamma Earth S2DR3 hosted API. It submits a job, follows the job's STAC collection and downloads
the products. It is **not** the Colab SDK (`ge-sdk`, which runs the model on your own GPU): this talks to the hosted API
with your API key.

> Status: 0.1 development version, tested against stage only. Nothing here is published to PyPI.

```python
from gamma_earth import Client

client = Client()                       # GE_API_URL and GE_API_KEY from the environment, or Client(api_url, api_key)
job = client.submit((30.906, 51.394, 30.936, 51.422), "2024-06-15")   # lon_min lat_min lon_max lat_max; spends credits
files = job.download("out/")            # waits for the product (minutes; the first job after a quiet spell is slower)
```

Step by step: `job.wait()` returns a `pystac.ItemCollection` (empty while the job runs); `client.download(collection, "out/",
assets=["tci"])` fetches chosen assets (names: `ms`, `tci`, `irp`, `ndvi`, and `*_10m` for the original-resolution files;
older products offer only `ms` and `tci`). To follow a job you already created: `client.job("<job id>")`.

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
