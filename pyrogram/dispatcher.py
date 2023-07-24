#  Pyrogram - Telegram MTProto API Client Library for Python
#  Copyright (C) 2017-present Dan <https://github.com/delivrance>
#
#  This file is part of Pyrogram.
#
#  Pyrogram is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Pyrogram is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with Pyrogram.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import inspect
import logging
from collections import OrderedDict

import pyrogram
from pyrogram import utils
from pyrogram.handlers import (
    CallbackQueryHandler, MessageHandler, EditedMessageHandler, DeletedMessagesHandler,
    UserStatusHandler, RawUpdateHandler, InlineQueryHandler, PollHandler, ErrorHandler,
    ChosenInlineResultHandler, ChatMemberUpdatedHandler, ChatJoinRequestHandler
)
from pyrogram.raw.types import (
    UpdateNewMessage, UpdateNewChannelMessage, UpdateNewScheduledMessage,
    UpdateEditMessage, UpdateEditChannelMessage,
    UpdateDeleteMessages, UpdateDeleteChannelMessages,
    UpdateBotCallbackQuery, UpdateInlineBotCallbackQuery,
    UpdateUserStatus, UpdateBotInlineQuery, UpdateMessagePoll,
    UpdateBotInlineSend, UpdateChatParticipant, UpdateChannelParticipant,
    UpdateBotChatInviteRequester
)

from pyrogram.fsm.fsm_helper import FSMData, FSMHelper

log = logging.getLogger(__name__)

class Dispatcher:
    NEW_MESSAGE_UPDATES = (UpdateNewMessage, UpdateNewChannelMessage, UpdateNewScheduledMessage)
    EDIT_MESSAGE_UPDATES = (UpdateEditMessage, UpdateEditChannelMessage)
    DELETE_MESSAGES_UPDATES = (UpdateDeleteMessages, UpdateDeleteChannelMessages)
    CALLBACK_QUERY_UPDATES = (UpdateBotCallbackQuery, UpdateInlineBotCallbackQuery)
    CHAT_MEMBER_UPDATES = (UpdateChatParticipant, UpdateChannelParticipant)
    USER_STATUS_UPDATES = (UpdateUserStatus,)
    BOT_INLINE_QUERY_UPDATES = (UpdateBotInlineQuery,)
    POLL_UPDATES = (UpdateMessagePoll,)
    CHOSEN_INLINE_RESULT_UPDATES = (UpdateBotInlineSend,)
    CHAT_JOIN_REQUEST_UPDATES = (UpdateBotChatInviteRequester,)

    def __init__(self, client: "pyrogram.Client"):
        self.client = client
        self.loop = asyncio.get_event_loop()

        self.handler_worker_tasks = []
        self.locks_list = []

        self.updates_queue = asyncio.Queue()
        self.groups = OrderedDict()
        self.error_handlers = []

        async def message_parser(update, users, chats):
            return (
                await pyrogram.types.Message._parse(self.client, update.message, users, chats,
                                                    isinstance(update, UpdateNewScheduledMessage)),
                MessageHandler
            )

        async def edited_message_parser(update, users, chats):
            # Edited messages are parsed the same way as new messages, but the handler is different
            parsed, _ = await message_parser(update, users, chats)

            return (
                parsed,
                EditedMessageHandler
            )

        async def deleted_messages_parser(update, users, chats):
            return (
                utils.parse_deleted_messages(self.client, update),
                DeletedMessagesHandler
            )

        async def callback_query_parser(update, users, chats):
            return (
                await pyrogram.types.CallbackQuery._parse(self.client, update, users),
                CallbackQueryHandler
            )

        async def user_status_parser(update, users, chats):
            return (
                pyrogram.types.User._parse_user_status(self.client, update),
                UserStatusHandler
            )

        async def inline_query_parser(update, users, chats):
            return (
                pyrogram.types.InlineQuery._parse(self.client, update, users),
                InlineQueryHandler
            )

        async def poll_parser(update, users, chats):
            return (
                pyrogram.types.Poll._parse_update(self.client, update),
                PollHandler
            )

        async def chosen_inline_result_parser(update, users, chats):
            return (
                pyrogram.types.ChosenInlineResult._parse(self.client, update, users),
                ChosenInlineResultHandler
            )

        async def chat_member_updated_parser(update, users, chats):
            return (
                pyrogram.types.ChatMemberUpdated._parse(self.client, update, users, chats),
                ChatMemberUpdatedHandler
            )

        async def chat_join_request_parser(update, users, chats):
            return (
                pyrogram.types.ChatJoinRequest._parse(self.client, update, users, chats),
                ChatJoinRequestHandler
            )

        self.update_parsers = {
            Dispatcher.NEW_MESSAGE_UPDATES: message_parser,
            Dispatcher.EDIT_MESSAGE_UPDATES: edited_message_parser,
            Dispatcher.DELETE_MESSAGES_UPDATES: deleted_messages_parser,
            Dispatcher.CALLBACK_QUERY_UPDATES: callback_query_parser,
            Dispatcher.USER_STATUS_UPDATES: user_status_parser,
            Dispatcher.BOT_INLINE_QUERY_UPDATES: inline_query_parser,
            Dispatcher.POLL_UPDATES: poll_parser,
            Dispatcher.CHOSEN_INLINE_RESULT_UPDATES: chosen_inline_result_parser,
            Dispatcher.CHAT_MEMBER_UPDATES: chat_member_updated_parser,
            Dispatcher.CHAT_JOIN_REQUEST_UPDATES: chat_join_request_parser
        }

        self.update_parsers = {key: value for key_tuple, value in self.update_parsers.items() for key in key_tuple}

    async def start(self):
        if not self.client.no_updates:
            for i in range(self.client.workers):
                self.locks_list.append(asyncio.Lock())

                self.handler_worker_tasks.append(
                    self.loop.create_task(self.handler_worker(self.locks_list[-1]))
                )

            log.info("Started %s HandlerTasks", self.client.workers)

    async def stop(self):
        if not self.client.no_updates:
            for i in range(self.client.workers):
                self.updates_queue.put_nowait(None)

            for i in self.handler_worker_tasks:
                await i

            self.handler_worker_tasks.clear()
            self.groups.clear()
            self.error_handlers.clear()

            log.info("Stopped %s HandlerTasks", self.client.workers)

    def add_handler(self, handler, group: int):
        async def fn():
            for lock in self.locks_list:
                await lock.acquire()

            try:
                if isinstance(handler, ErrorHandler):
                    if handler not in self.error_handlers:
                        self.error_handlers.append(handler)
                else:
                    if group not in self.groups:
                        self.groups[group] = []
                        self.groups = OrderedDict(sorted(self.groups.items()))

                    self.groups[group].append(handler)
            finally:
                for lock in self.locks_list:
                    lock.release()

        self.loop.create_task(fn())

    def remove_handler(self, handler, group: int):
        async def fn():
            for lock in self.locks_list:
                await lock.acquire()

            try:
                if isinstance(handler, ErrorHandler):
                    if handler not in self.error_handlers:
                        raise ValueError(f"Error handler {handler} does not exist. Handler was not removed.")

                    self.error_handlers.remove(handler)
                else:
                    if group not in self.groups:
                        raise ValueError(f"Group {group} does not exist. Handler was not removed.")

                    self.groups[group].remove(handler)
            finally:
                for lock in self.locks_list:
                    lock.release()

        self.loop.create_task(fn())


    async def handler_worker(self, lock):
        while True:
            packet = await self.updates_queue.get()

            if packet is None:
                break

            try:
                update, users, chats = packet
                parser = self.update_parsers.get(type(update), None)

                parsed_updates, handler_type = (
                    await parser(update, users, chats)
                    if parser is not None
                    else (None, type(None))
                )

                if parsed_updates is None:
                    continue

                fsm_helper = FSMHelper()
                FSMData.include_helper_to_pool(parsed_updates, fsm_helper)

                if FSMData.pyrogram_patch_fsm_storage:
                    await fsm_helper._include_state(
                        parsed_updates, FSMData.pyrogram_patch_fsm_storage, self.client
                    )

                # process outer middlewares
                for middleware in FSMData.pyrogram_patch_outer_middlewares:
                    if middleware == handler_type:
                        await fsm_helper._process_middleware(
                            parsed_updates, middleware, self.client
                        )

                async with lock:
                    for group in self.groups.values():
                        for handler in group:
                            args = None

                            if isinstance(handler, handler_type):
                                try:
                                    # filtering event
                                    if await handler.check(self.client, parsed_updates):
                                        # process middlewares
                                        for middleware in FSMData.pyrogram_patch_middlewares:
                                            if middleware == type(handler):
                                                await fsm_helper._process_middleware(
                                                    parsed_updates,
                                                    middleware,
                                                    self.client,
                                                )
                                        args = (parsed_updates,)
                                except Exception as e:
                                    log.exception(e)
                                    FSMData.exclude_helper_from_pool(parsed_updates)
                                    continue

                            elif isinstance(handler, RawUpdateHandler):
                                try:
                                    # process middlewares
                                    for middleware in FSMData.pyrogram_patch_middlewares:
                                        if middleware == type(handler):
                                            await fsm_helper._process_middleware(
                                                parsed_updates,
                                                middleware,
                                                self.client,
                                            )
                                    args = (update, users, chats)
                                except pyrogram.StopPropagation:
                                    FSMData.exclude_helper_from_pool(parsed_updates)
                                    continue
                            if args is None:
                                continue

                            try:
                                # formation kwargs
                                kwargs = await fsm_helper._get_data_for_handler(
                                    handler.callback.__code__.co_varnames
                                )
                                if inspect.iscoroutinefunction(handler.callback):
                                    await handler.callback(self.client, *args, **kwargs)
                                else:
                                    args = list(args)
                                    for v in kwargs.values():
                                        args.append(v)
                                    args = tuple(args)
                                    await self.loop.run_in_executor(
                                        self.client.executor,
                                        handler.callback,
                                        self.client,
                                        *args
                                    )
                            except pyrogram.StopPropagation:
                                raise
                            except pyrogram.ContinuePropagation:
                                continue
                            except Exception as e:
                                handled_error = False
                                for error_handler in self.error_handlers:
                                    try:
                                        if await error_handler.check(self.client, e):
                                            if inspect.iscoroutinefunction(error_handler.callback):
                                                await error_handler.callback(self.client, e)
                                            else:
                                                await self.loop.run_in_executor(
                                                    self.client.executor,
                                                    error_handler.callback,
                                                    self.client,
                                                    e
                                                )
                                            handled_error = True  # If the error is handled, don't log it
                                            break
                                    except pyrogram.StopPropagation:
                                        raise
                                    except pyrogram.ContinuePropagation:
                                        continue
                                    except Exception as e:
                                        log.exception(e)
                                        continue

                                if not handled_error:
                                    log.exception(e)
                            finally:
                                FSMData.exclude_helper_from_pool(parsed_updates)
                            break
                    FSMData.exclude_helper_from_pool(parsed_updates)
            except pyrogram.StopPropagation:
                pass
            except Exception as e:
                log.exception(e)
