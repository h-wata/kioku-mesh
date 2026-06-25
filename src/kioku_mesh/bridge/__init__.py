"""Bridge layer: messaging-to-memory promotion bridge (ADR-0023, Phase 4).

memory と messaging の双方向依存を防ぐ中継層。
bridge だけが messaging と memory の両方を import できる。
"""

from kioku_mesh.bridge.message_memory import MessageMemoryBridge

__all__ = ['MessageMemoryBridge']
