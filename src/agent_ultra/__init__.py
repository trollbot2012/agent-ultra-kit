"""agent-ultra-kit — adversarial panel, ULTRA loop, command broker, and proof
gates for any agent runtime.

Core principle: panel agents are ROLES (prompt-scoped critic lenses), not
models. One healthy model route can run a whole panel; extra routes add
backend diversity, never a hard requirement.
"""

__version__ = "0.1.0"

from .broker.broker import (  # noqa: F401
    SAFE,
    ELEVATED,
    DANGEROUS,
    TRUSTED_OWNER_TIERS,
    CRITIC_TIERS,
    CommandBroker,
    BrokerResult,
    classify,
)
from .panel.engine import PanelEngine, PanelError, PanelReport, Finding  # noqa: F401
from .routes.pool import RoutePool, RouteError  # noqa: F401
from .routes.client import OpenAIChatClient  # noqa: F401
from .routes.mock import MockChatClient, demo_panel_client  # noqa: F401
from .ultra_loop.loop import UltraLoop, UltraReport  # noqa: F401
from .proof.gates import ProofGate, GateSet, ProofError  # noqa: F401
from .memory.hooks import MemoryHooks, CompositeHooks, JsonlHooks  # noqa: F401
from .receipts_bus import (  # noqa: F401
    ReceiptsBus,
    BusUnavailable,
    Candidate,
    build_envelope,
    EnvelopeError,
)
from .verifier import (  # noqa: F401
    verify_claim,
    Verifier,
    VerifierResult,
    EscalationBudget,
    BudgetExceeded,
)
