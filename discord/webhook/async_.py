"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import contextvars
import logging
import asyncio
import json
import time
import re

from urllib.parse import quote as urlquote
from typing import Any, Dict, List, Literal, NamedTuple, Optional, TYPE_CHECKING, Tuple, Union, overload

import aiohttp

from .. import utils
from ..errors import InvalidArgument, HTTPException, Forbidden, NotFound, DiscordServerError
from ..message import Message
from ..enums import try_enum, WebhookType
from ..user import BaseUser, User
from ..asset import Asset
from ..http import Route
from ..mixins import Hashable
from ..object import Object

__all__ = (
    'Webhook',
    'WebhookMessage',
    'PartialWebhookChannel',
    'PartialWebhookGuild',
)

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..file import File
    from ..embeds import Embed
    from ..mentions import AllowedMentions
    from ..types.webhook import (
        Webhook as WebhookPayload,
    )
    from ..guild import Guild
    from ..channel import TextChannel
    from ..abc import Snowflake
    import datetime


class _Missing:
    def __bool__(self):
        return False

    def __repr__(self):
        return '...'


MISSING: Any = _Missing()


class AsyncDeferredLock:
    def __init__(self, lock: asyncio.Lock):
        self.lock = lock
        self.delta: Optional[float] = None

    async def __aenter__(self):
        await self.lock.acquire()
        return self

    def delay_by(self, delta: float) -> None:
        self.delta = delta

    async def __aexit__(self, type, value, traceback):
        if self.delta:
            await asyncio.sleep(self.delta)
        self.lock.release()


class AsyncWebhookAdapter:
    def __init__(self):
        self._locks: Dict[Any, asyncio.Lock] = {}

    async def request(
        self,
        route: Route,
        session: aiohttp.ClientSession,
        *,
        payload: Optional[Dict[str, Any]] = None,
        multipart: Optional[List[Dict[str, Any]]] = None,
        files: Optional[List[File]] = None,
        reason: Optional[str] = None,
        auth_token: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        headers: Dict[str, str] = {}
        files = files or []
        to_send: Optional[Union[str, aiohttp.FormData]] = None
        bucket = (route.webhook_id, route.webhook_token)

        try:
            lock = self._locks[bucket]
        except KeyError:
            self._locks[bucket] = lock = asyncio.Lock()

        if payload is not None:
            headers['Content-Type'] = 'application/json'
            to_send = utils.to_json(payload)

        if auth_token is not None:
            headers['Authorization'] = f'Bot {auth_token}'

        if reason is not None:
            headers['X-Audit-Log-Reason'] = urlquote(reason, safe='/ ')

        response: Optional[aiohttp.ClientResponse] = None
        data: Optional[Union[Dict[str, Any], str]] = None
        method = route.method
        url = route.url
        webhook_id = route.webhook_id

        async with AsyncDeferredLock(lock) as lock:
            for attempt in range(5):
                for file in files:
                    file.reset(seek=attempt)

                if multipart:
                    form_data = aiohttp.FormData()
                    for p in multipart:
                        form_data.add_field(**p)
                    to_send = form_data

                try:
                    async with session.request(method, url, data=to_send, headers=headers, params=params) as response:
                        log.debug(
                            'Webhook ID %s with %s %s has returned status code %s',
                            webhook_id,
                            method,
                            url,
                            response.status,
                        )
                        data = (await response.text(encoding='utf-8')) or None
                        if data and response.headers['Content-Type'] == 'application/json':
                            data = json.loads(data)

                        remaining = response.headers.get('X-Ratelimit-Remaining')
                        if remaining == '0' and response.status != 429:
                            delta = utils._parse_ratelimit_header(response)
                            log.debug(
                                'Webhook ID %s has been pre-emptively rate limited, waiting %.2f seconds', webhook_id, delta
                            )
                            lock.delay_by(delta)

                        if 300 > response.status >= 200:
                            return data

                        if response.status == 429:
                            if not response.headers.get('Via'):
                                raise HTTPException(response, data)

                            retry_after: float = data['retry_after']  # type: ignore
                            log.warning('Webhook ID %s is rate limited. Retrying in %.2f seconds', webhook_id, retry_after)
                            await asyncio.sleep(retry_after)
                            continue

                        if response.status >= 500:
                            await asyncio.sleep(1 + attempt * 2)
                            continue

                        if response.status == 403:
                            raise Forbidden(response, data)
                        elif response.status == 404:
                            raise NotFound(response, data)
                        else:
                            raise HTTPException(response, data)

                except OSError as e:
                    if attempt < 4 and e.errno in (54, 10054):
                        await asyncio.sleep(1 + attempt * 2)
                        continue
                    raise

            if response:
                if response.status >= 500:
                    raise DiscordServerError(response, data)
                raise HTTPException(response, data)

            raise RuntimeError('Unreachable code in HTTP handling.')

    def delete_webhook(
        self,
        webhook_id: int,
        *,
        token: Optional[str] = None,
        session: aiohttp.ClientSession,
        reason: Optional[str] = None,
    ):
        route = Route('DELETE', '/webhooks/{webhook_id}', webhook_id=webhook_id)
        return self.request(route, session, reason=reason, auth_token=token)

    def delete_webhook_with_token(
        self,
        webhook_id: int,
        token: str,
        *,
        session: aiohttp.ClientSession,
        reason: Optional[str] = None,
    ):
        route = Route('DELETE', '/webhooks/{webhook_id}/{webhook_token}', webhook_id=webhook_id, webhook_token=token)
        return self.request(route, session, reason=reason)

    def edit_webhook(
        self,
        webhook_id: int,
        token: str,
        payload: Dict[str, Any],
        *,
        session: aiohttp.ClientSession,
        reason: Optional[str] = None,
    ):
        route = Route('PATCH', '/webhooks/{webhook_id}', webhook_id=webhook_id)
        return self.request(route, session, reason=reason, payload=payload, auth_token=token)

    def edit_webhook_with_token(
        self,
        webhook_id: int,
        token: str,
        payload: Dict[str, Any],
        *,
        session: aiohttp.ClientSession,
        reason: Optional[str] = None,
    ):
        route = Route('PATCH', '/webhooks/{webhook_id}/{webhook_token}', webhook_id=webhook_id, webhook_token=token)
        return self.request(route, session, reason=reason, payload=payload)

    def execute_webhook(
        self,
        webhook_id: int,
        token: str,
        *,
        session: aiohttp.ClientSession,
        payload: Optional[Dict[str, Any]] = None,
        multipart: Optional[List[Dict[str, Any]]] = None,
        files: Optional[List[File]] = None,
        wait: bool = False,
    ):
        params = {'wait': int(wait)}
        route = Route('POST', '/webhooks/{webhook_id}/{webhook_token}', webhook_id=webhook_id, webhook_token=token)
        return self.request(route, session, payload=payload, multipart=multipart, files=files, params=params)

    def get_webhook_message(
        self,
        webhook_id: int,
        token: str,
        message_id: int,
        *,
        session: aiohttp.ClientSession,
    ):
        route = Route(
            'GET',
            '/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}',
            webhook_id=webhook_id,
            webhook_token=token,
            message_id=message_id,
        )
        return self.request(route, session)

    def edit_webhook_message(
        self,
        webhook_id: int,
        token: str,
        message_id: int,
        *,
        session: aiohttp.ClientSession,
        payload: Optional[Dict[str, Any]] = None,
        multipart: Optional[List[Dict[str, Any]]] = None,
        files: Optional[List[File]] = None,
    ):
        route = Route(
            'PATCH',
            '/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}',
            webhook_id=webhook_id,
            webhook_token=token,
            message_id=message_id,
        )
        return self.request(route, session, payload=payload, multipart=multipart, files=files)

    def delete_webhook_message(
        self,
        webhook_id: int,
        token: str,
        message_id: int,
        *,
        session: aiohttp.ClientSession,
    ):
        route = Route(
            'DELETE',
            '/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}',
            webhook_id=webhook_id,
            webhook_token=token,
            message_id=message_id,
        )
        return self.request(route, session)

    def fetch_webhook(
        self,
        webhook_id: int,
        token: str,
        *,
        session: aiohttp.ClientSession,
    ):
        route = Route('GET', '/webhooks/{webhook_id}', webhook_id=webhook_id)
        return self.request(route, session=session, auth_token=token)

    def fetch_webhook_with_token(
        self,
        webhook_id: int,
        token: str,
        *,
        session: aiohttp.ClientSession,
    ):
        route = Route('GET', '/webhooks/{webhook_id}/{webhook_token}', webhook_id=webhook_id, webhook_token=token)
        return self.request(route, session=session)


class ExecuteWebhookParameters(NamedTuple):
    payload: Optional[Dict[str, Any]]
    multipart: Optional[List[Dict[str, Any]]]
    files: Optional[List[File]]


def handle_message_parameters(
    content: Optional[str] = MISSING,
    *,
    username: str = MISSING,
    avatar_url: str = MISSING,
    tts: bool = False,
    file: File = MISSING,
    files: List[File] = MISSING,
    embed: Optional[Embed] = MISSING,
    embeds: List[Embed] = MISSING,
    allowed_mentions: Optional[AllowedMentions] = MISSING,
    previous_allowed_mentions: Optional[AllowedMentions] = None,
) -> ExecuteWebhookParameters:
    if files is not MISSING and file is not MISSING:
        raise TypeError('Cannot mix file and files keyword arguments.')
    if embeds is not MISSING and embed is not MISSING:
        raise TypeError('Cannot mix embed and embeds keyword arguments.')

    payload = {}
    if embeds is not MISSING:
        if len(embeds) > 10:
            raise InvalidArgument('embeds has a maximum of 10 elements.')
        payload['embeds'] = [e.to_dict() for e in embeds]

    if embed is not MISSING:
        if embed is None:
            payload['embeds'] = []
        else:
            payload['embeds'] = [embed.to_dict()]

    if content is not MISSING:
        if content is not None:
            payload['content'] = str(content)
        else:
            payload['content'] = None

    payload['tts'] = tts
    if avatar_url:
        payload['avatar_url'] = str(avatar_url)
    if username:
        payload['username'] = username

    if allowed_mentions:
        if previous_allowed_mentions is not None:
            payload['allowed_mentions'] = previous_allowed_mentions.merge(allowed_mentions).to_dict()
        else:
            payload['allowed_mentions'] = allowed_mentions.to_dict()
    elif previous_allowed_mentions is not None:
        payload['allowed_mentions'] = previous_allowed_mentions.to_dict()

    multipart = []
    if file is not MISSING:
        files = [file]

    if files:
        multipart.append({'name': 'payload_json', 'value': utils.to_json(payload)})
        payload = None
        if len(files) == 1:
            file = files[0]
            multipart.append(
                {
                    'name': 'file',
                    'value': file.fp,
                    'filename': file.filename,
                    'content_type': 'application/octet-stream',
                }
            )
        else:
            for index, file in enumerate(files):
                multipart.append(
                    {
                        'name': f'file{index}',
                        'value': file.fp,
                        'filename': file.filename,
                        'content_type': 'application/octet-stream',
                    }
                )

    return ExecuteWebhookParameters(payload=payload, multipart=multipart, files=files)


async_context = contextvars.ContextVar('async_webhook_context', default=AsyncWebhookAdapter())


class PartialWebhookChannel(Hashable):
    """Represents a partial channel for webhooks.

    These are typically given for channel follower webhooks.

    .. versionadded:: 2.0

    Attributes
    -----------
    id: :class:`int`
        The partial channel's ID.
    name: :class:`str`
        The partial channel's name.
    """

    __slots__ = ('id', 'name')

    def __init__(self, *, data):
        self.id = int(data['id'])
        self.name = data['name']

    def __repr__(self):
        return f'<PartialWebhookChannel name={self.name!r} id={self.id}>'


class PartialWebhookGuild(Hashable):
    """Represents a partial guild for webhooks.

    These are typically given for channel follower webhooks.

    .. versionadded:: 2.0

    Attributes
    -----------
    id: :class:`int`
        The partial guild's ID.
    name: :class:`str`
        The partial guild's name.
    icon: :class:`str`
        The partial guild's icon
    """

    __slots__ = ('id', 'name', 'icon', '_state')

    def __init__(self, *, data, state):
        self._state = state
        self.id = int(data['id'])
        self.name = data['name']
        self.icon = data['icon']

    def __repr__(self):
        return f'<PartialWebhookGuild name={self.name!r} id={self.id}>'

    @property
    def icon_url(self) -> Asset:
        """:class:`Asset`: Returns the guild's icon asset."""
        return self.icon_url_as()

    def is_icon_animated(self) -> bool:
        """:class:`bool`: Returns True if the guild has an animated icon."""
        return bool(self.icon and self.icon.startswith('a_'))

    def icon_url_as(self, *, format=None, static_format='webp', size=1024):
        """Returns an :class:`Asset` for the guild's icon.

        The format must be one of 'webp', 'jpeg', 'jpg', 'png' or 'gif', and
        'gif' is only valid for animated avatars. The size must be a power of 2
        between 16 and 4096.

        Parameters
        -----------
        format: Optional[:class:`str`]
            The format to attempt to convert the icon to.
            If the format is ``None``, then it is automatically
            detected into either 'gif' or static_format depending on the
            icon being animated or not.
        static_format: Optional[:class:`str`]
            Format to attempt to convert only non-animated icons to.
        size: :class:`int`
            The size of the image to display.

        Raises
        ------
        InvalidArgument
            Bad image format passed to ``format`` or invalid ``size``.

        Returns
        --------
        :class:`Asset`
            The resulting CDN asset.
        """
        return Asset._from_guild_icon(self._state, self, format=format, static_format=static_format, size=size)


class _FriendlyHttpAttributeErrorHelper:
    __slots__ = ()

    def __getattr__(self, attr):
        raise AttributeError('PartialWebhookState does not support http methods.')


class _WebhookState:
    __slots__ = ('_parent', '_webhook')

    def __init__(self, webhook, parent):
        self._webhook = webhook

        if isinstance(parent, self.__class__):
            self._parent = None
        else:
            self._parent = parent

    def _get_guild(self, guild_id):
        if self._parent is not None:
            return self._parent._get_guild(guild_id)
        return None

    def store_user(self, data):
        if self._parent is not None:
            return self._parent.store_user(data)
        return BaseUser(state=self, data=data)

    @property
    def http(self):
        if self._parent is not None:
            return self._parent.http

        # Some data classes assign state.http and that should be kosher
        # however, using it should result in a late-binding error.
        return _FriendlyHttpAttributeErrorHelper()

    def __getattr__(self, attr):
        if self._parent is not None:
            return getattr(self._parent, attr)

        raise AttributeError(f'PartialWebhookState does not support {attr!r}.')


class WebhookMessage(Message):
    """Represents a message sent from your webhook.

    This allows you to edit or delete a message sent by your
    webhook.

    This inherits from :class:`discord.Message` with changes to
    :meth:`edit` and :meth:`delete` to work.

    .. versionadded:: 1.6
    """

    async def edit(
        self,
        content: Optional[str] = MISSING,
        embeds: List[Embed] = MISSING,
        embed: Optional[Embed] = MISSING,
        file: File = MISSING,
        files: List[File] = MISSING,
        allowed_mentions: Optional[AllowedMentions] = None,
    ):
        """|coro|

        Edits the message.

        .. versionadded:: 1.6

        Parameters
        ------------
        content: Optional[:class:`str`]
            The content to edit the message with or ``None`` to clear it.
        embeds: List[:class:`Embed`]
            A list of embeds to edit the message with.
        embed: Optional[:class:`Embed`]
            The embed to edit the message with. ``None`` suppresses the embeds.
            This should not be mixed with the ``embeds`` parameter.
        file: :class:`File`
            The file to upload. This cannot be mixed with ``files`` parameter.
        files: List[:class:`File`]
            A list of files to send with the content. This cannot be mixed with the
            ``file`` parameter.
        allowed_mentions: :class:`AllowedMentions`
            Controls the mentions being processed in this message.
            See :meth:`.abc.Messageable.send` for more information.

        Raises
        -------
        HTTPException
            Editing the message failed.
        Forbidden
            Edited a message that is not yours.
        TypeError
            You specified both ``embed`` and ``embeds`` or ``file`` and ``files``
        ValueError
            The length of ``embeds`` was invalid
        InvalidArgument
            There was no token associated with this webhook.
        """
        await self._state._webhook.edit_message(
            self.id,
            content=content,
            embeds=embeds,
            embed=embed,
            file=file,
            files=files,
            allowed_mentions=allowed_mentions,
        )

    async def delete(self, *, delay: Optional[float] = None) -> None:
        """|coro|

        Deletes the message.

        Parameters
        -----------
        delay: Optional[:class:`float`]
            If provided, the number of seconds to wait before deleting the message.
            The waiting is done in the background and deletion failures are ignored.

        Raises
        ------
        Forbidden
            You do not have proper permissions to delete the message.
        NotFound
            The message was deleted already.
        HTTPException
            Deleting the message failed.
        """

        if delay is not None:

            async def inner_call(delay: float = delay):
                await asyncio.sleep(delay)
                try:
                    await self._state._webhook.delete_message(self.id)
                except HTTPException:
                    pass

            asyncio.create_task(inner_call())
        else:
            await self._state._webhook.delete_message(self.id)


class BaseWebhook(Hashable):
    __slots__: Tuple[str, ...] = (
        'id',
        'type',
        'guild_id',
        'channel_id',
        'token',
        'auth_token',
        'user',
        'name',
        'avatar',
        'source_channel',
        'source_guild',
        '_state',
    )

    def __init__(self, data: WebhookPayload, token: Optional[str] = None, state=None):
        self.auth_token: Optional[str] = token
        self._state = state or _WebhookState(self, parent=state)
        self._update(data)

    def _update(self, data: WebhookPayload):
        self.id = int(data['id'])
        self.type = try_enum(WebhookType, int(data['type']))
        self.channel_id = utils._get_as_snowflake(data, 'channel_id')
        self.guild_id = utils._get_as_snowflake(data, 'guild_id')
        self.name = data.get('name')
        self.avatar = data.get('avatar')
        self.token = data.get('token')

        user = data.get('user')
        self.user: Optional[Union[BaseUser, User]] = None
        if user is not None:
            if self._state is None:
                self.user = BaseUser(state=None, data=user)
            else:
                self.user = User(state=self._state, data=user)

        source_channel = data.get('source_channel')
        if source_channel:
            source_channel = PartialWebhookChannel(data=source_channel)

        self.source_channel: Optional[PartialWebhookChannel] = source_channel

        source_guild = data.get('source_guild')
        if source_guild:
            source_guild = PartialWebhookGuild(data=source_guild, state=self._state)

        self.source_guild: Optional[PartialWebhookGuild] = source_guild

    def is_partial(self) -> bool:
        """:class:`bool`: Whether the webhook is a "partial" webhook."""
        return self.channel_id is None

    def is_authenticated(self) -> bool:
        """:class:`bool`: Whether the webhook is authenticated with a bot token."""
        return self.auth_token is not None

    @property
    def guild(self) -> Optional[Guild]:
        """Optional[:class:`Guild`]: The guild this webhook belongs to.

        If this is a partial webhook, then this will always return ``None``.
        """
        return self._state and self._state._get_guild(self.guild_id)

    @property
    def channel(self) -> Optional[TextChannel]:
        """Optional[:class:`TextChannel`]: The text channel this webhook belongs to.

        If this is a partial webhook, then this will always return ``None``.
        """
        guild = self.guild
        return guild and guild.get_channel(self.channel_id)

    @property
    def created_at(self) -> datetime.datetime:
        """:class:`datetime.datetime`: Returns the webhook's creation time in UTC."""
        return utils.snowflake_time(self.id)

    @property
    def avatar_url(self) -> Asset:
        """:class:`Asset`: Returns an :class:`Asset` for the avatar the webhook has.

        If the webhook does not have a traditional avatar, an asset for
        the default avatar is returned instead.

        This is equivalent to calling :meth:`avatar_url_as` with the
        default parameters.
        """
        return self.avatar_url_as()

    def avatar_url_as(self, *, format: Optional[Literal['png', 'jpg', 'jpeg']] = None, size: int = 1024) -> Asset:
        """Returns an :class:`Asset` for the avatar the webhook has.

        If the webhook does not have a traditional avatar, an asset for
        the default avatar is returned instead.

        The format must be one of 'jpeg', 'jpg', or 'png'.
        The size must be a power of 2 between 16 and 1024.

        Parameters
        -----------
        format: Optional[:class:`str`]
            The format to attempt to convert the avatar to.
            If the format is ``None``, then it is equivalent to png.
        size: :class:`int`
            The size of the image to display.

        Raises
        ------
        InvalidArgument
            Bad image format passed to ``format`` or invalid ``size``.

        Returns
        --------
        :class:`Asset`
            The resulting CDN asset.
        """
        if self.avatar is None:
            # Default is always blurple apparently
            return Asset(self._state, '/embed/avatars/0.png')

        if not utils.valid_icon_size(size):
            raise InvalidArgument("size must be a power of 2 between 16 and 1024")

        format = format or 'png'

        if format not in ('png', 'jpg', 'jpeg'):
            raise InvalidArgument("format must be one of 'png', 'jpg', or 'jpeg'.")

        url = f'/avatars/{self.id}/{self.avatar}.{format}?size={size}'
        return Asset(self._state, url)


class Webhook(BaseWebhook):
    """Represents an asynchronous Discord webhook.

    Webhooks are a form to send messages to channels in Discord without a
    bot user or authentication.

    There are two main ways to use Webhooks. The first is through the ones
    received by the library such as :meth:`.Guild.webhooks` and
    :meth:`.TextChannel.webhooks`. The ones received by the library will
    automatically be bound using the library's internal HTTP session.

    The second form involves creating a webhook object manually using the
    :meth:`~.Webhook.from_url` or :meth:`~.Webhook.partial` classmethods.

    For example, creating a webhook from a URL and using :doc:`aiohttp <aio:index>`:

    .. code-block:: python3

        from discord import Webhook, AsyncWebhookAdapter
        import aiohttp

        async def foo():
            async with aiohttp.ClientSession() as session:
                webhook = Webhook.from_url('url-here', session=session)
                await webhook.send('Hello World', username='Foo')

    For a synchronous counterpart, see :class:`SyncWebhook`.

    .. container:: operations

        .. describe:: x == y

            Checks if two webhooks are equal.

        .. describe:: x != y

            Checks if two webhooks are not equal.

        .. describe:: hash(x)

            Returns the webhooks's hash.

    .. versionchanged:: 1.4
        Webhooks are now comparable and hashable.

    Attributes
    ------------
    id: :class:`int`
        The webhook's ID
    type: :class:`WebhookType`
        The type of the webhook.

        .. versionadded:: 1.3

    token: Optional[:class:`str`]
        The authentication token of the webhook. If this is ``None``
        then the webhook cannot be used to make requests.
    guild_id: Optional[:class:`int`]
        The guild ID this webhook is for.
    channel_id: Optional[:class:`int`]
        The channel ID this webhook is for.
    user: Optional[:class:`abc.User`]
        The user this webhook was created by. If the webhook was
        received without authentication then this will be ``None``.
    name: Optional[:class:`str`]
        The default name of the webhook.
    avatar: Optional[:class:`str`]
        The default avatar of the webhook.
    source_guild: Optional[:class:`PartialWebhookGuild`]
        The guild of the channel that this webhook is following.
        Only given if :attr:`type` is :attr:`WebhookType.channel_follower`.

        .. versionadded:: 2.0

    source_channel: Optional[:class:`PartialWebhookChannel`]
        The channel that this webhook is following.
        Only given if :attr:`type` is :attr:`WebhookType.channel_follower`.

        .. versionadded:: 2.0
    """

    __slots__: Tuple[str, ...] = BaseWebhook.__slots__ + ('session',)

    def __init__(self, data: WebhookPayload, session: aiohttp.ClientSession, token: Optional[str] = None, state=None):
        super().__init__(data, token, state)
        self.session = session

    def __repr__(self):
        return f'<Webhook id={self.id!r}>'

    @property
    def url(self):
        """:class:`str` : Returns the webhook's url."""
        return f'https://discord.com/api/webhooks/{self.id}/{self.token}'

    @classmethod
    def partial(cls, id: int, token: str, *, session: aiohttp.ClientSession, bot_token: Optional[str] = None):
        """Creates a partial :class:`Webhook`.

        Parameters
        -----------
        id: :class:`int`
            The ID of the webhook.
        token: :class:`str`
            The authentication token of the webhook.
        session: :class:`aiohttp.ClientSession`
            The session to use to send requests with. Note
            that the library does not manage the session and
            will not close it.
        bot_token: Optional[:class:`str`]
            The bot authentication token for authenticated requests
            involving the webhook.

        Returns
        --------
        :class:`Webhook`
            A partial :class:`Webhook`.
            A partial webhook is just a webhook object with an ID and a token.
        """
        data: WebhookPayload = {
            'id': id,
            'type': 1,
            'token': token,
        }

        return cls(data, session, token=bot_token)

    @classmethod
    def from_url(cls, url: str, *, session: aiohttp.ClientSession, bot_token: Optional[str] = None):
        """Creates a partial :class:`Webhook` from a webhook URL.

        Parameters
        ------------
        url: :class:`str`
            The URL of the webhook.
        session: :class:`aiohttp.ClientSession`
            The session to use to send requests with. Note
            that the library does not manage the session and
            will not close it.
        bot_token: Optional[:class:`str`]
            The bot authentication token for authenticated requests
            involving the webhook.

        Raises
        -------
        InvalidArgument
            The URL is invalid.

        Returns
        --------
        :class:`Webhook`
            A partial :class:`Webhook`.
            A partial webhook is just a webhook object with an ID and a token.
        """
        m = re.search(r'discord(?:app)?.com/api/webhooks/(?P<id>[0-9]{17,20})/(?P<token>[A-Za-z0-9\.\-\_]{60,68})', url)
        if m is None:
            raise InvalidArgument('Invalid webhook URL given.')

        data: Dict[str, Any] = m.groupdict()
        data['type'] = 1
        return cls(data, session, token=bot_token)  # type: ignore

    @classmethod
    def _as_follower(cls, data, *, channel, user):
        name = f"{channel.guild} #{channel}"
        feed: WebhookPayload = {
            'id': data['webhook_id'],
            'type': 2,
            'name': name,
            'channel_id': channel.id,
            'guild_id': channel.guild.id,
            'user': {'username': user.name, 'discriminator': user.discriminator, 'id': user.id, 'avatar': user.avatar},
        }

        state = channel._state
        session = channel._state.http._HTTPClient__session
        return cls(feed, session=session, state=state, token=state.http.token)

    @classmethod
    def from_state(cls, data, state):
        session = state.http._HTTPClient__session
        return cls(data, session=session, state=state, token=state.http.token)

    async def fetch(self, *, prefer_auth: bool = True) -> Webhook:
        """|coro|

        Fetches the current webhook.

        This could be used to get a full webhook from a partial webhook.

        .. note::

            When fetching with an unauthenticated webhook, i.e.
            :meth:`is_authenticated` returns ``False``, then the
            returned webhook does not contain any user information.

        Parameters
        -----------
        prefer_auth: :class:`bool`
            Whether to use the bot token over the webhook token
            if available. Defaults to ``True``.

        Raises
        -------
        HTTPException
            Could not fetch the webhook
        NotFound
            Could not find the webhook by this ID
        InvalidArgument
            This webhook does not have a token associated with it.

        Returns
        --------
        :class:`Webhook`
            The fetched webhook.
        """
        adapter = async_context.get()

        if prefer_auth and self.auth_token:
            data = await adapter.fetch_webhook(self.id, self.auth_token, session=self.session)
        elif self.token:
            data = await adapter.fetch_webhook_with_token(self.id, self.token, session=self.session)
        else:
            raise InvalidArgument('This webhook does not have a token associated with it')

        return Webhook(data, self.session, token=self.auth_token, state=self._state)

    async def delete(self, *, reason: Optional[str] = None, prefer_auth: bool = True):
        """|coro|

        Deletes this Webhook.

        Parameters
        ------------
        reason: Optional[:class:`str`]
            The reason for deleting this webhook. Shows up on the audit log.

            .. versionadded:: 1.4
        prefer_auth: :class:`bool`
            Whether to use the bot token over the webhook token
            if available. Defaults to ``True``.

        Raises
        -------
        HTTPException
            Deleting the webhook failed.
        NotFound
            This webhook does not exist.
        Forbidden
            You do not have permissions to delete this webhook.
        InvalidArgument
            This webhook does not have a token associated with it.
        """
        if self.token is None and self.auth_token is None:
            raise InvalidArgument('This webhook does not have a token associated with it')

        adapter = async_context.get()

        if prefer_auth and self.auth_token:
            await adapter.delete_webhook(self.id, token=self.auth_token, session=self.session, reason=reason)
        elif self.token:
            await adapter.delete_webhook_with_token(self.id, self.token, session=self.session, reason=reason)

    async def edit(
        self,
        *,
        reason: Optional[str] = None,
        name: Optional[str] = MISSING,
        avatar: Optional[bytes] = MISSING,
        channel: Optional[Snowflake] = None,
        prefer_auth: bool = True,
    ):
        """|coro|

        Edits this Webhook.

        Parameters
        ------------
        name: Optional[:class:`str`]
            The webhook's new default name.
        avatar: Optional[:class:`bytes`]
            A :term:`py:bytes-like object` representing the webhook's new default avatar.
        channel: Optional[:class:`abc.Snowflake`]
            The webhook's new channel. This requires an authenticated webhook.
        reason: Optional[:class:`str`]
            The reason for editing this webhook. Shows up on the audit log.

            .. versionadded:: 1.4
        prefer_auth: :class:`bool`
            Whether to use the bot token over the webhook token
            if available. Defaults to ``True``.

        Raises
        -------
        HTTPException
            Editing the webhook failed.
        NotFound
            This webhook does not exist.
        InvalidArgument
            This webhook does not have a token associated with it
            or it tried editing a channel without authentication.
        """
        if self.token is None and self.auth_token is None:
            raise InvalidArgument('This webhook does not have a token associated with it')

        payload = {}
        if name is not MISSING:
            payload['name'] = str(name) if name is not None else None

        if avatar is not MISSING:
            payload['avatar'] = utils._bytes_to_base64_data(avatar) if avatar is not None else None

        adapter = async_context.get()

        # If a channel is given, always use the authenticated endpoint
        if channel is not None:
            if self.auth_token is None:
                raise InvalidArgument('Editing channel requires authenticated webhook')

            payload['channel_id'] = channel.id
            data = await adapter.edit_webhook(self.id, self.auth_token, payload=payload, session=self.session, reason=reason)
            self._update(data)
            return

        if prefer_auth and self.auth_token:
            data = await adapter.edit_webhook(self.id, self.auth_token, payload=payload, session=self.session, reason=reason)
            self._update(data)
        elif self.token:
            data = await adapter.edit_webhook_with_token(
                self.id, self.token, payload=payload, session=self.session, reason=reason
            )
            self._update(data)

    def _create_message(self, data):
        state = _WebhookState(self, parent=self._state)
        channel = self.channel or Object(id=int(data['channel_id']))
        return WebhookMessage(data=data, state=state, channel=channel)

    @overload
    async def send(
        self,
        content: str = MISSING,
        *,
        username: str = MISSING,
        avatar_url: str = MISSING,
        tts: bool = MISSING,
        file: File = MISSING,
        files: List[File] = MISSING,
        embed: Embed = MISSING,
        embeds: List[Embed] = MISSING,
        allowed_mentions: AllowedMentions = MISSING,
        wait: Literal[True],
    ) -> WebhookMessage:
        ...

    @overload
    async def send(
        self,
        content: str = MISSING,
        *,
        username: str = MISSING,
        avatar_url: str = MISSING,
        tts: bool = MISSING,
        file: File = MISSING,
        files: List[File] = MISSING,
        embed: Embed = MISSING,
        embeds: List[Embed] = MISSING,
        allowed_mentions: AllowedMentions = MISSING,
        wait: Literal[False] = ...,
    ) -> None:
        ...

    async def send(
        self,
        content: str = MISSING,
        *,
        username: str = MISSING,
        avatar_url: str = MISSING,
        tts: bool = False,
        file: File = MISSING,
        files: List[File] = MISSING,
        embed: Embed = MISSING,
        embeds: List[Embed] = MISSING,
        allowed_mentions: AllowedMentions = MISSING,
        wait: bool = False,
    ) -> Optional[WebhookMessage]:
        """|coro|

        Sends a message using the webhook.

        The content must be a type that can convert to a string through ``str(content)``.

        To upload a single file, the ``file`` parameter should be used with a
        single :class:`File` object.

        If the ``embed`` parameter is provided, it must be of type :class:`Embed` and
        it must be a rich embed type. You cannot mix the ``embed`` parameter with the
        ``embeds`` parameter, which must be a :class:`list` of :class:`Embed` objects to send.

        Parameters
        ------------
        content: :class:`str`
            The content of the message to send.
        wait: :class:`bool`
            Whether the server should wait before sending a response. This essentially
            means that the return type of this function changes from ``None`` to
            a :class:`WebhookMessage` if set to ``True``.
        username: :class:`str`
            The username to send with this message. If no username is provided
            then the default username for the webhook is used.
        avatar_url: Union[:class:`str`, :class:`Asset`]
            The avatar URL to send with this message. If no avatar URL is provided
            then the default avatar for the webhook is used.
        tts: :class:`bool`
            Indicates if the message should be sent using text-to-speech.
        file: :class:`File`
            The file to upload. This cannot be mixed with ``files`` parameter.
        files: List[:class:`File`]
            A list of files to send with the content. This cannot be mixed with the
            ``file`` parameter.
        embed: :class:`Embed`
            The rich embed for the content to send. This cannot be mixed with
            ``embeds`` parameter.
        embeds: List[:class:`Embed`]
            A list of embeds to send with the content. Maximum of 10. This cannot
            be mixed with the ``embed`` parameter.
        allowed_mentions: :class:`AllowedMentions`
            Controls the mentions being processed in this message.

            .. versionadded:: 1.4

        Raises
        --------
        HTTPException
            Sending the message failed.
        NotFound
            This webhook was not found.
        Forbidden
            The authorization token for the webhook is incorrect.
        TypeError
            You specified both ``embed`` and ``embeds`` or ``file`` and ``files``
        ValueError
            The length of ``embeds`` was invalid
        InvalidArgument
            There was no token associated with this webhook.

        Returns
        ---------
        Optional[:class:`WebhookMessage`]
            The message that was sent.
        """

        if self.token is None:
            raise InvalidArgument('This webhook does not have a token associated with it')

        previous_mentions: Optional[AllowedMentions] = getattr(self._state, 'allowed_mentions', None)
        if content is None:
            content = ...  # type: ignore

        params = handle_message_parameters(
            content=content,
            username=username,
            avatar_url=avatar_url,
            tts=tts,
            file=file,
            files=files,
            embed=embed,
            embeds=embeds,
            allowed_mentions=allowed_mentions,
            previous_allowed_mentions=previous_mentions,
        )
        adapter = async_context.get()
        data = await adapter.execute_webhook(
            self.id,
            self.token,
            session=self.session,
            payload=params.payload,
            multipart=params.multipart,
            files=params.files,
            wait=wait,
        )
        if wait:
            return self._create_message(data)

    async def fetch_message(self, id: int) -> WebhookMessage:
        """|coro|

        Retrieves a single :class:`~discord.WebhookMessage` owned by this webhook.

        .. versionadded:: 2.0

        Parameters
        ------------
        id: :class:`int`
            The message ID to look for.

        Raises
        --------
        ~discord.NotFound
            The specified message was not found.
        ~discord.Forbidden
            You do not have the permissions required to get a message.
        ~discord.HTTPException
            Retrieving the message failed.
        InvalidArgument
            There was no token associated with this webhook.

        Returns
        --------
        :class:`~discord.WebhookMessage`
            The message asked for.
        """

        if self.token is None:
            raise InvalidArgument('This webhook does not have a token associated with it')

        adapter = async_context.get()
        data = await adapter.get_webhook_message(
            self.id,
            self.token,
            id,
            session=self.session,
        )
        return self._create_message(data)

    async def edit_message(
        self,
        message_id: int,
        *,
        content: Optional[str] = MISSING,
        embeds: List[Embed] = MISSING,
        embed: Optional[Embed] = MISSING,
        file: File = MISSING,
        files: List[File] = MISSING,
        allowed_mentions: Optional[AllowedMentions] = None,
    ):
        """|coro|

        Edits a message owned by this webhook.

        This is a lower level interface to :meth:`WebhookMessage.edit` in case
        you only have an ID.

        .. versionadded:: 1.6

        Parameters
        ------------
        message_id: :class:`int`
            The message ID to edit.
        content: Optional[:class:`str`]
            The content to edit the message with or ``None`` to clear it.
        embeds: List[:class:`Embed`]
            A list of embeds to edit the message with.
        embed: Optional[:class:`Embed`]
            The embed to edit the message with. ``None`` suppresses the embeds.
            This should not be mixed with the ``embeds`` parameter.
        file: :class:`File`
            The file to upload. This cannot be mixed with ``files`` parameter.
        files: List[:class:`File`]
            A list of files to send with the content. This cannot be mixed with the
            ``file`` parameter.
        allowed_mentions: :class:`AllowedMentions`
            Controls the mentions being processed in this message.
            See :meth:`.abc.Messageable.send` for more information.

        Raises
        -------
        HTTPException
            Editing the message failed.
        Forbidden
            Edited a message that is not yours.
        TypeError
            You specified both ``embed`` and ``embeds`` or ``file`` and ``files``
        ValueError
            The length of ``embeds`` was invalid
        InvalidArgument
            There was no token associated with this webhook.
        """

        if self.token is None:
            raise InvalidArgument('This webhook does not have a token associated with it')

        previous_mentions: Optional[AllowedMentions] = getattr(self._state, 'allowed_mentions', None)
        params = handle_message_parameters(
            content=content,
            file=file,
            files=files,
            embed=embed,
            embeds=embeds,
            allowed_mentions=allowed_mentions,
            previous_allowed_mentions=previous_mentions,
        )
        adapter = async_context.get()
        await adapter.edit_webhook_message(
            self.id,
            self.token,
            message_id,
            session=self.session,
            payload=params.payload,
            multipart=params.multipart,
            files=params.files,
        )

    async def delete_message(self, message_id: int):
        """|coro|

        Deletes a message owned by this webhook.

        This is a lower level interface to :meth:`WebhookMessage.delete` in case
        you only have an ID.

        .. versionadded:: 1.6

        Parameters
        ------------
        message_id: :class:`int`
            The message ID to delete.

        Raises
        -------
        HTTPException
            Deleting the message failed.
        Forbidden
            Deleted a message that is not yours.
        """
        if self.token is None:
            raise InvalidArgument('This webhook does not have a token associated with it')

        adapter = async_context.get()
        await adapter.delete_webhook_message(
            self.id,
            self.token,
            message_id,
            session=self.session,
        )