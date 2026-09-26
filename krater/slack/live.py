"""`LiveSlackClient`: `SlackClient` backed by the real Slack Web API, via `slack_sdk`.

Every call that's meant to be idempotent (`create_channel`, `invite_users`, `archive_channel`) treats
the Slack error code that means "already in the state we wanted" as success, per `docs/SPEC.md`
("Invites and posts must be safe to retry: `already_in_channel` counts as success...").

`SlackApiError` (Slack understood the request but it failed) and the broader `SlackClientError` (a
network/transport problem, e.g. `SlackRequestError`) are handled separately -- `SlackApiError` is
itself a `SlackClientError`, so it's always caught first.
"""

from __future__ import annotations

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.webhook import WebhookClient

from krater.config import Settings
from krater.slack.errors import SlackRequestFailedError, SlackUnavailableError
from krater.slack.types import SlackUserInfo

#: `conversations.invite`/`conversations.archive` error codes that mean "already done" -- treated as
#: success so a retried job (or a channel someone joined manually in between) doesn't fail.
_ALREADY_IN_CHANNEL_ERRORS = frozenset({"already_in_channel"})
_ALREADY_ARCHIVED_ERRORS = frozenset({"already_archived"})


def _slack_error_code(exc: SlackApiError) -> str | None:
    try:
        return exc.response.get("error")
    except AttributeError:
        return None


def _request_failed(method: str, exc: SlackApiError) -> SlackRequestFailedError:
    return SlackRequestFailedError(f"{method} failed: {_slack_error_code(exc) or exc}")


def _unavailable(method: str, exc: SlackClientError) -> SlackUnavailableError:
    return SlackUnavailableError(f"could not reach Slack for {method}: {exc}")


class LiveSlackClient:
    """A `SlackClient` (see `krater.slack.client`) backed by a real Slack workspace. `web_client` is
    injectable for tests; production code leaves it out and gets a real `slack_sdk.WebClient`."""

    def __init__(self, settings: Settings, *, web_client: WebClient | None = None) -> None:
        self._settings = settings
        self._client = web_client if web_client is not None else WebClient(token=settings.slack_bot_token)

    # -- Channels --------------------------------------------------------------------------------------

    def create_channel(self, name: str) -> str:
        try:
            response = self._client.conversations_create(name=name, is_private=True)
        except SlackApiError as exc:
            if _slack_error_code(exc) == "name_taken":
                existing = self._find_channel_by_name(name)
                if existing is not None:
                    return existing
            raise _request_failed("conversations.create", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("conversations.create", exc) from exc
        return response["channel"]["id"]

    def _find_channel_by_name(self, name: str) -> str | None:
        """Best-effort lookup for the `name_taken` case: someone (or a previous, half-finished attempt)
        already created a channel with this name. Paginates through every private channel Krater's bot
        can see."""
        cursor: str | None = None
        while True:
            try:
                response = self._client.conversations_list(
                    types="private_channel", exclude_archived=True, limit=200, cursor=cursor or None
                )
            except SlackApiError as exc:
                raise _request_failed("conversations.list", exc) from exc
            except SlackClientError as exc:
                raise _unavailable("conversations.list", exc) from exc
            for channel in response.get("channels", []):
                if channel.get("name") == name:
                    return channel["id"]
            cursor = response.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                return None

    def invite_users(self, channel_id: str, slack_user_ids: list[str]) -> None:
        if not slack_user_ids:
            return
        try:
            self._client.conversations_invite(channel=channel_id, users=slack_user_ids)
        except SlackApiError as exc:
            if _slack_error_code(exc) in _ALREADY_IN_CHANNEL_ERRORS:
                return
            raise _request_failed("conversations.invite", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("conversations.invite", exc) from exc

    def archive_channel(self, channel_id: str) -> None:
        try:
            self._client.conversations_archive(channel=channel_id)
        except SlackApiError as exc:
            if _slack_error_code(exc) in _ALREADY_ARCHIVED_ERRORS:
                return
            raise _request_failed("conversations.archive", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("conversations.archive", exc) from exc

    # -- Messages --------------------------------------------------------------------------------------

    def post_message(self, channel_id: str, *, blocks: list[dict], text: str) -> str:
        try:
            response = self._client.chat_postMessage(channel=channel_id, blocks=blocks, text=text)
        except SlackApiError as exc:
            raise _request_failed("chat.postMessage", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("chat.postMessage", exc) from exc
        return response["ts"]

    def update_message(self, channel_id: str, ts: str, *, blocks: list[dict], text: str) -> None:
        try:
            self._client.chat_update(channel=channel_id, ts=ts, blocks=blocks, text=text)
        except SlackApiError as exc:
            raise _request_failed("chat.update", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("chat.update", exc) from exc

    def open_view(self, trigger_id: str, view: dict) -> None:
        try:
            self._client.views_open(trigger_id=trigger_id, view=view)
        except SlackApiError as exc:
            raise _request_failed("views.open", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("views.open", exc) from exc

    def post_ephemeral_via_response_url(self, response_url: str, text: str) -> None:
        try:
            response = WebhookClient(response_url).send(text=text, response_type="ephemeral")
        except SlackClientError as exc:
            raise _unavailable("response_url", exc) from exc
        if response.status_code >= 400:
            raise SlackRequestFailedError(f"response_url post failed: {response.status_code} {response.body}")

    # -- Directory -------------------------------------------------------------------------------------

    def lookup_user_by_email(self, email: str) -> str | None:
        try:
            response = self._client.users_lookupByEmail(email=email)
        except SlackApiError as exc:
            if _slack_error_code(exc) == "users_not_found":
                return None
            raise _request_failed("users.lookupByEmail", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("users.lookupByEmail", exc) from exc
        return response["user"]["id"]

    def get_user_info(self, slack_user_id: str) -> SlackUserInfo | None:
        try:
            response = self._client.users_info(user=slack_user_id)
        except SlackApiError as exc:
            if _slack_error_code(exc) == "user_not_found":
                return None
            raise _request_failed("users.info", exc) from exc
        except SlackClientError as exc:
            raise _unavailable("users.info", exc) from exc
        user = response["user"]
        return SlackUserInfo(
            slack_id=user["id"],
            deleted=bool(user.get("deleted", False)),
            is_restricted=bool(user.get("is_restricted", False)),
            is_ultra_restricted=bool(user.get("is_ultra_restricted", False)),
        )


__all__ = ["LiveSlackClient"]
