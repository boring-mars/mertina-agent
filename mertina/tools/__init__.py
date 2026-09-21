# Ported from hermes-agent tools/__init__.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Tools package namespace. Kept side-effect free: importing ``tools`` must not
load the tool stack (some subsystems import it while ``hermes_cli.config`` is
still initializing). Import concrete submodules directly."""
