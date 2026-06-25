"""Memory layer: observation data management — store, index, queue, and purge (ADR-0023).

store・local_index・pending_queue・purge・backend 等を含む。
core 層に依存してよいが、messaging・bridge 層には依存しない。
"""
