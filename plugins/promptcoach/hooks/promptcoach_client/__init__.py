"""Client side of the prompt coach: an agent-neutral core plus one adapter per agent.

``core`` and the modules it uses know nothing about any particular agent; an
adapter module turns its agent's hook payloads into the neutral events in
``core`` and the coach's replies into its agent's hook output. Standard
library only, Python 3.10 or newer.
"""

__version__ = "0.1.0"
