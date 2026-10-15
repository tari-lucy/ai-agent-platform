"""Explicit local host commands: prepare a data directory once, then serve it STOPPED.

Importing this package performs no IO. Nothing here reads a cwd-relative path, a `.env`
or the process environment.
"""
