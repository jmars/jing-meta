"""Vibe maintenance hooks for jing-meta.

Thin ``post_agent`` hook wrappers around the dreamer and archiver engines,
exposed as console scripts (``jing-gardener-hook``, ``jing-archiver-hook``,
``jing-index-hook``, ``jing-maintenance-digest``). These are the glue that
wires autonomous
maintenance into Vibe; see each module for the hook design.
"""
