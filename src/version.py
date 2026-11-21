"""Product and data-format versions, stamped into every saved result so a file can be interpreted (and trusted)
long after the code that wrote it has changed."""
__version__ = "1.3.0"
# Version of the saved-result file layout (logs/classic/*.json). Bump when fields change meaning.
RESULT_SCHEMA = 2
