"""One module per provider.

A new provider is a subclass of ``Channel`` that declares its fields and
implements ``deliver``. Registering it in ``notifications.KINDS`` is the only
other step — the store, the API, the interface and the translations all follow
from the class.
"""
