import json
import logging
import os
import sys
import time
import threading
import atexit
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

_active_environments: Dict[str, Any] = {}
_env_lock = threading.Lock()
def _resolve_container_task_id(task_id: Optional[str]) -> str:
    """Map a tool-call ``task_id`` to the ``_active_environments`` key. Order matters —
    earlier branches are authoritative where they apply:

    1. Image/``env_type`` overrides (RL/benchmark rollouts) key their own sandbox;
       CWD-only overrides (ACP workspace tracking) are NOT isolation signals.
    2. Per-session isolation (docker + ``container_persistent: false``): each
       session's task_id is its own key (a fresh chat gets a fresh sandbox with only
       ITS mounts); delegate_task children follow the alias registry to the parent.
    3. Session key present (WebUI per-session, gateway per-message): persistent
       Docker is PROFILE-scoped — ``shared:<key>`` opt-in, else ``profile:<name>``,
       with the default profile staying literally ``"default"`` so CLI and
       default-profile gateway sessions share ONE container; other backends key
       ``session:<key>`` so switching profiles can't reuse another profile's
       SSHEnvironment on the wrong host.
    4. No session key (CLI): ``shared:<key>`` when opted in (else a CLI run of a
       keyed profile would split from its gateway sessions), else ``"default"``,
       which subagent ids collapse onto to share the parent's container.
    """
    return None
