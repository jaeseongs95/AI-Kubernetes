"""AIK-045 desktop engine: the stdio JSON-lines backend of the desktop GUI shell.

The Electron main process starts one engine per window session and talks to it
over the inherited stdin/stdout pipes. The engine only reads: it reports what
it observes about this machine and the non-authoritative read model, and it
never invents a value it did not observe. It holds no credentials.
"""

from .server import COMMANDS, ENGINE_VERSION, PROTOCOL_VERSION, Engine, EngineConfig, main, serve

__all__ = ["COMMANDS", "ENGINE_VERSION", "PROTOCOL_VERSION", "Engine", "EngineConfig", "main", "serve"]
