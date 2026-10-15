"""Host resource readings (disk, memory, load, processes) from outside sources.

Kelpie has no collector of its own: node_exporter runs on every host and Prometheus
scrapes it. Everything here is optional and off unless ``[resources.prometheus]`` is
configured. The package builds on ``kelpie.core`` only.
"""
