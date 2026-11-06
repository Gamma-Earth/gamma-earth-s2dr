"""The client tags every job with the INSTALLED version of the package (`gamma-earth-s2dr/<version>`). A checkout that was not
pip-installed has no version to read, which would make the tests that look at the tag fail for no good reason. Give them a
fixed one, before the package is imported, only when the real one is missing."""
from importlib import metadata

_real_version = metadata.version


def _version(name):
    try:
        return _real_version(name)
    except metadata.PackageNotFoundError:
        if name == 'gamma-earth-s2dr':
            return '0.0.0.test'
        raise


metadata.version = _version
