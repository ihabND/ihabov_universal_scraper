"""A local SOCKS5 proxy that connects only to public addresses.

Both fetchers send their traffic through it unless an upstream proxy is configured (which can't reach
this network). Redirects, subresources, WebSockets and DNS rebinding are then all checked when the
connection is made, against the address actually connected to.
"""

import asyncio
import logging
import socket

from ..core.errors import ScrapeError
from . import urls

CONNECT_TIMEOUT_SECONDS = 10
GRANTED = b"\x05\x00\x00\x01" + bytes(6)
REFUSED = b"\x05\x02\x00\x01" + bytes(6)  # "connection not allowed by ruleset"

logger = logging.getLogger(__name__)


class EgressGuard:
    def __init__(self):
        self.server: asyncio.Server | None = None
        self.address = ""

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.address = f"127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            host, port = await read_connect_request(reader, writer)
            try:
                upstream_reader, upstream_writer = await connect(await urls.resolve_public(host), port)
            except (ScrapeError, OSError) as e:
                logger.info("Refused a connection to %s:%d: %s", host, port, e)
                writer.write(REFUSED)
                return
            writer.write(GRANTED)
            await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
        except (asyncio.IncompleteReadError, OSError, ValueError):
            pass
        finally:
            writer.close()


async def connect(ips: list[str], port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """The first address that answers, as a browser would do it."""
    error = OSError("no address")
    for ip in ips:
        try:
            return await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT_SECONDS)
        except OSError as e:  # includes the timeout
            error = e
    raise error


async def read_connect_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[str, int]:
    _, method_count = await reader.readexactly(2)
    await reader.readexactly(method_count)
    writer.write(b"\x05\x00")  # no authentication
    _, command, _, address_type = await reader.readexactly(4)
    if address_type == 1:
        host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
    elif address_type == 4:
        host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
    else:
        host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
    port = int.from_bytes(await reader.readexactly(2), "big")
    if command != 1:  # only CONNECT; no UDP or listening sockets
        raise ValueError(f"SOCKS command {command}")
    return host, port


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()
