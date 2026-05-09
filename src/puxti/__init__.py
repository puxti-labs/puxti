from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("puxti")
except PackageNotFoundError:
    __version__ = "dev"
