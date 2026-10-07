# Ported from hermes-agent tools/environments/__init__.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Mertina execution environment backends: one BaseEnvironment ABC for running shell commands
in a specific context (local, Docker, SSH, Singularity, Modal direct/Nous-managed, Daytona,
Vercel Sandbox). ``terminal_tool._create_environment`` selects the backend from TERMINAL_ENV."""

from mertina.tools.environments.base import BaseEnvironment
