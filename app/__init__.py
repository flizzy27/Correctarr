"""Correctarr.

The version is written here and nowhere else. It used to come from the build,
which meant that a container built from a branch rather than a tag reported
itself as ``main-`` followed by a forty character commit hash — a string long
enough to run straight out of the sidebar and across the page behind it.

What the build passes in is still kept, under ``BUILD``: it says which image
this is, which matters when something has to be reproduced. It belongs in the
tooltip, not in the width of a column.
"""
__version__ = "1.1.2"
