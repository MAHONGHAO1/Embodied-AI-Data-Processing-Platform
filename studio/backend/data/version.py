"""QuicStudio product and compatibility version constants.

``__version__`` is the single source of the product version. It follows PEP 440
(``1.0.0a1`` is the first alpha of 1.0.0); pyproject.toml reads it and the web
console shows it through ``GET /api/v1/version``.
"""

__version__ = "1.0.0a1"

PRODUCT_VERSION = __version__
API_COMPATIBILITY_VERSION = "v1"
DATABASE_BASELINE = "0001_v0_2_baseline"
