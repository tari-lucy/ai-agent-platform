"""Bounded single-group Redis Streams. IDs only; no pending-entry trimming."""

import asyncio
import re
from typing import Any

from ai_agent_application.jobs.ports import Delivery, DispatchRef
from ai_agent_contracts.codec import ContractError, from_wire, to_wire
from ai_agent_contracts.common import Id, Scope
from ai_agent_contracts.states import ErrorCode
from pydantic import TypeAdapter, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

PUBLISH = """
if redis.call('XLEN', KEYS[1]) >= tonumber(ARGV[1]) then return false end
return redis.call('XADD', KEYS[1], '*',
'event_id',ARGV[2],'operation_id',ARGV[3],'resource_id',ARGV[4],
'resource_version',ARGV[5],'scope',ARGV[6])
"""
ACK = """
local groups = redis.call('XINFO','GROUPS',KEYS[1])
if #groups ~= 1 then return -1 end
local ack = redis.call('XACK',KEYS[1],ARGV[1],ARGV[2])
if ack == 1 then redis.call('XDEL',KEYS[1],ARGV[2]) end
return ack
"""


class RedisStreams:
    def __init__(
        self,
        client: Redis,
        *,
        stream: str,
        group: str,
        capacity: int = 256,
        timeout_seconds: float = 2,
    ) -> None:
        if not 1 <= capacity <= 256 or not 0 < timeout_seconds <= 5:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        self._id(stream)
        self._id(group)
        self.client, self.stream, self.group = client, stream, group
        self.capacity, self.timeout = capacity, timeout_seconds
        self.cursor = "0-0"

    @staticmethod
    def _id(value: str) -> None:
        try:
            TypeAdapter(Id).validate_python(value)
        except ValidationError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None

    async def prepare(self) -> None:
        """Explicit administrative setup for one group, idempotent after broker loss."""
        try:
            async with asyncio.timeout(self.timeout):
                try:
                    await self.client.xgroup_create(
                        self.stream, self.group, id="0-0", mkstream=True
                    )
                except ResponseError as error:
                    if not str(error).startswith("BUSYGROUP"):
                        raise
                groups = await self.client.xinfo_groups(self.stream)
                if len(groups) != 1:
                    raise ContractError(ErrorCode.ACCESS_DENIED)
        except RedisError, TimeoutError:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE) from None

    async def publish(self, reference: DispatchRef) -> str:
        self._id(reference.event_id)
        self._id(reference.operation_id)
        self._id(reference.resource_id)
        if not 1 <= reference.resource_version:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        try:
            async with asyncio.timeout(self.timeout):
                result = await self.client.eval(
                    PUBLISH,
                    1,
                    self.stream,
                    self.capacity,
                    reference.event_id,
                    reference.operation_id,
                    reference.resource_id,
                    reference.resource_version,
                    to_wire(reference.scope).decode(),
                )
            if not result:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            return str(result.decode() if isinstance(result, bytes) else result)
        except RedisError, TimeoutError:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE) from None

    def _deliveries(self, entries: Any) -> list[Delivery]:
        items = []
        for message_id, fields in entries:
            data = {
                key.decode() if isinstance(key, bytes) else key: value.decode()
                if isinstance(value, bytes)
                else value
                for key, value in fields.items()
            }
            if set(data) != {
                "event_id",
                "operation_id",
                "resource_id",
                "resource_version",
                "scope",
            }:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            try:
                for name in ("event_id", "operation_id", "resource_id"):
                    self._id(data[name])
                version = int(data["resource_version"])
                if not 1 <= version:
                    raise ValueError
                scope = from_wire(TypeAdapter(Scope), data["scope"].encode())
            except ValueError, ValidationError:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
            items.append(
                Delivery(
                    message_id.decode() if isinstance(message_id, bytes) else str(message_id),
                    DispatchRef(
                        data["event_id"],
                        data["operation_id"],
                        data["resource_id"],
                        version,
                        scope,
                    ),
                )
            )
        return items

    async def read(self, consumer: str, *, count: int = 1) -> list[Delivery]:
        self._id(consumer)
        if not 1 <= count <= 200:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        try:
            async with asyncio.timeout(self.timeout):
                entries = await self.client.xreadgroup(
                    self.group,
                    consumer,
                    {self.stream: ">"},
                    count=count,
                )
            if isinstance(entries, dict):
                return self._deliveries(next(iter(entries.values()))) if entries else []
            wire: Any = entries
            return self._deliveries(wire[0][1]) if wire else []
        except RedisError, TimeoutError:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE) from None

    async def reclaim(
        self,
        consumer: str,
        *,
        idle_ms: int,
        count: int = 1,
    ) -> list[Delivery]:
        self._id(consumer)
        if not 1 <= count <= 200 or not 1 <= idle_ms <= 180000:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.client.xautoclaim(
                    self.stream,
                    self.group,
                    consumer,
                    idle_ms,
                    self.cursor,
                    count=count,
                )
            self.cursor = response[0]
            return self._deliveries(response[1])
        except RedisError, TimeoutError:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE) from None

    async def acknowledge(self, delivery_id: str) -> None:
        if re.fullmatch(r"[0-9]{1,20}-[0-9]{1,20}", delivery_id) is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        try:
            async with asyncio.timeout(self.timeout):
                result = await self.client.eval(ACK, 1, self.stream, self.group, delivery_id)
            if result == -1:
                raise ContractError(ErrorCode.ACCESS_DENIED)
        except RedisError, TimeoutError:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE) from None
