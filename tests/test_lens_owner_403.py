"""The caller-surface denial discloses nothing — not even existence.

Any distinction between "denied" and "absent" — status code OR body text — is
an existence oracle over business-describing lens names, so a 403 naming the
lens owner is a leak even though it tells a denied caller who to ask. The
owner hint stays off the caller surface. The denial still names the next move
(the org's dst admin), which is safe because it is the same for every name.
"""

from __future__ import annotations

from services.api.query import _lens_unavailable


def test_denial_is_a_404_that_does_not_confirm_existence() -> None:
    exc = _lens_unavailable("sales")
    assert exc.status_code == 404
    assert "may not exist" in exc.detail
    assert "may not be granted" in exc.detail


def test_denial_body_is_byte_identical_for_every_probed_name() -> None:
    # Not even an echo of the name: two probes can never be diffed against
    # each other, so denied and absent are indistinguishable.
    assert _lens_unavailable("sales").detail == _lens_unavailable("no_such_lens").detail
    assert "sales" not in _lens_unavailable("sales").detail


def test_denial_names_the_next_move_without_naming_the_owner() -> None:
    exc = _lens_unavailable("sales")
    assert "dst admin" in exc.detail
    assert "owner" not in exc.detail
