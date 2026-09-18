from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("puxti")
except PackageNotFoundError:
    __version__ = "dev"
