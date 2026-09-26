"""The interim Slack membership gate: `docs/SPEC.md` "Roles & authentication" ("Slack membership gate
(interim)").

Signing in through Weave doesn't guarantee Slack membership -- Weave signup is open, and new users join
Slack as single-channel guests until they accept the code of conduct. Until Weave exposes Slack
membership as a claim ([weave#118](https://github.com/patchworklabsorg/weave/issues/118)), Krater checks
Slack itself, on submit.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.models import User
from krater.slack.client import SlackClient


def is_full_slack_member(session: Session, slack_client: SlackClient, user: User) -> bool:
    """Whether `user` is a full (non-guest, active) member of the Patchwork Labs Slack.

    Uses `User.slack_user_id` if already known (from Weave's `slack_id` claim, or a previous call to
    this function); otherwise falls back to `users.lookupByEmail` and, if that resolves, caches the
    result onto `user.slack_user_id` so future calls (and Slack invites) don't need the lookup again.

    `False` if no Slack account can be found at all, or if the account is deleted, `is_restricted`
    (a single/multi-channel guest) or `is_ultra_restricted` (a single-channel guest) -- see
    `krater.slack.types.SlackUserInfo`. Flushes (but doesn't commit) when it caches a discovered id.
    """
    slack_id = user.slack_user_id
    if slack_id is None:
        slack_id = slack_client.lookup_user_by_email(user.email)
        if slack_id is None:
            return False
        user.slack_user_id = slack_id
        session.flush()

    info = slack_client.get_user_info(slack_id)
    if info is None:
        return False
    return not (info.deleted or info.is_restricted or info.is_ultra_restricted)


__all__ = ["is_full_slack_member"]
