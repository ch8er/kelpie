"""Platform- and CI-agnostic core: domain model, rule engine, storage, alerts.

Nothing in this package may import ``kelpie.adapters`` or ``kelpie.connectors``.
Integrations implement the protocols in :mod:`kelpie.core.ports` instead.
"""
