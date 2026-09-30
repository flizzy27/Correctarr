"""The HTTP interface, one router per area.

What the routers share — the store, the engine, the sign-in check and the
translated errors — lives in :mod:`app.api.core`; what runs on a clock or on a
webhook in :mod:`app.api.jobs`. ``app.main`` puts them together.
"""
