import asyncio
import json
import logging
from typing import AsyncGenerator, Optional
from core.jsonrpc import parse_jsonrpc_message

logger = logging.getLogger(__name__)

class StdioTransport:
    def __init__(self, command: list[str]):
        self.command = command
        self.process: Optional[asyncio.subprocess.Process] = None

    async def start(self):
        """Starts the subprocess and attaches to stdin/stdout."""
        logger.debug(f"Starting subprocess: {' '.join(self.command)}")
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # We start a task to log stderr independently
        asyncio.create_task(self._read_stderr())

    async def _read_stderr(self):
        """Reads stderr from the subprocess and logs it."""
        if not self.process or not self.process.stderr:
            return
        async for line in self.process.stderr:
            logger.error(f"[Subprocess STDERR]: {line.decode().strip()}")

    async def send_message(self, message: dict):
        """Sends a JSON-RPC message to the subprocess via stdin."""
        if not self.process or not self.process.stdin:
            raise RuntimeError("Subprocess not started or stdin unavailable")
        
        payload = json.dumps(message)
        logger.debug(f"--> Sending: {payload}")
        self.process.stdin.write((payload + "\n").encode())
        await self.process.stdin.drain()

    async def read_messages(self) -> AsyncGenerator[dict, None]:
        """Reads JSON-RPC messages from the subprocess via stdout."""
        if not self.process or not self.process.stdout:
            raise RuntimeError("Subprocess not started or stdout unavailable")
        
        async for line in self.process.stdout:
            try:
                line_str = line.decode().strip()
                if not line_str:
                    continue
                logger.debug(f"<-- Received: {line_str}")
                data = json.loads(line_str)
                yield data
            except json.JSONDecodeError:
                logger.warning(f"Failed to decode JSON from stdout: {line.decode().strip()}")

    async def close(self):
        """Terminates the subprocess."""
        if self.process:
            self.process.terminate()
            await self.process.wait()
            self.process = None
            logger.debug("Subprocess terminated.")
