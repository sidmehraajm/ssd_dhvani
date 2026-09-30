"""ssd_dhvani — audio recording, review & publishing tool for an AYON pipeline
(the sound twin of ssd_abhinaya). Runs on Windows and macOS."""

#: Full version. Bump the last part for fixes, the middle for features.
__version__ = "1.3.1"

#: Short tag used in the executable name (ssd_dhvani_v1.exe) and anywhere a
#: compact build id is wanted. Only changes on a MAJOR release, so a v1 build
#: always replaces the previous v1 build on the operators' machines.
VERSION_TAG = "v" + __version__.split(".")[0]

#: What the title bar / login screen / logs show.
VERSION_LABEL = f"{VERSION_TAG} ({__version__})"
