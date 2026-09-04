"""The one place the version number lives.

Rewritten in CI by scripts/set_version.py from the git tag being released, so
a build's version is the tag by construction rather than by somebody
remembering to bump a constant in three files. See .github/workflows/release.yml.

The committed value is deliberately a .dev version rather than a plausible
release number: a source checkout is not a release, and it should say so
rather than claiming to be whatever the last tag happened to be. The updater
is inert from source anyway (updater.can_update() is False), so nothing
compares this against GitHub.
"""

from __future__ import annotations

__version__ = "0.0.0.dev0"
