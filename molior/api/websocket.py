import asyncio
import json

from pathlib import Path
from aiofile import AIOFile, Reader

from molior.app import app, logger
from molior.molior.notifier import Subject, Event, Action
from molior.model.database import Session
from molior.model.build import Build

BUILD_OUT_PATH = Path("/var/lib/molior/buildout")


class LiveLogger:
    """
    Provides helper functions for livelogging on molior.
    """

    def __init__(self, sender, build_id):
        self.__sender = sender
        self.build_id = build_id
        self.__up = False
        self.__filepath = BUILD_OUT_PATH / str(build_id) / "build.log"

    def stop(self):
        """
        Stops the livelogging
        """
        logger.info("build-{}: stopping livelogger".format(self.build_id))
        self.__up = False

    async def start(self):
        """
        Starts the livelogging
        """
        logger.info("build-{}: starting livelogger".format(self.build_id))
        self.__up = True
        while self.__up:
            try:
                async with AIOFile(str(self.__filepath), "r") as log_file:
                    reader = Reader(log_file, chunk_size=16384)
                    while self.__up:
                        async for data in reader:
                            message = {"event": Event.added.value, "subject": Subject.buildlog.value, "data": data}
                            await self.__sender(json.dumps(message))

                        # EOF
                        with Session() as session:
                            build = session.query(Build).filter(Build.id == self.build_id).first()
                            if not build:
                                logger.error("build: build %d not found", self.build_id)
                                self.stop()
                                continue
                            if build.buildstate != "building" and   \
                               build.buildstate != "publishing" and \
                               build.buildstate != "needs_publish":
                                logger.info("buildlog: end of build {}".format(self.build_id))
                                self.stop()
                                continue
                        await asyncio.sleep(1)
                        continue
            except FileNotFoundError:
                logger.error("livelogger: log file not found: {}".format(self.__filepath))
                await asyncio.sleep(1)
            except Exception as exc:
                logger.error("livelogger: error sending live logs")
                logger.exception(exc)
                await asyncio.sleep(1)


async def start_livelogger(websocket, data):
    """
    Starts the livelogger for the given
    websocket client.

    Args:
        websocket: The websocket instance.
        data (dict): The received data.
    """
    if "build_id" not in data:
        logger.error("livelogger: no build ID found")
        return False

    llogger = LiveLogger(websocket.send_str, data.get("build_id"))

    if hasattr(websocket, "logger") and websocket.logger:
        logger.error("livelogger: removeing existing livelogger")
        await stop_livelogger(websocket, data)

    websocket.logger = llogger
    loop = asyncio.get_event_loop()
    loop.create_task(llogger.start())


async def stop_livelogger(websocket, _):
    """
    Stops the livelogger.
    """
    if hasattr(websocket, "logger") and websocket.logger:
        websocket.logger.stop()
    else:
        logger.error("stop_livelogger: no active logger found")


async def dispatch(websocket, message):
    """
    Dispatchers websocket requests to different
    handler functions.

    Args:
        websocket: The websocket instance.
        message (dict): The received message dict.

    Returns:
        bool: True if successful, False otherwise.
    """
    handlers = {
        Subject.buildlog.value: {
            Action.start.value: start_livelogger,
            Action.stop.value: stop_livelogger,
        }
    }

    if "subject" not in message or "action" not in message:
        logger.error("unknown websocket message recieved: {}".format(message))
        return False

    handler = handlers.get(message.get("subject")).get(message.get("action"))
    await handler(websocket, message.get("data"))
    return True


@app.websocket_connect()
async def websocket_connected(websocket):
    """
    Sends a `success` message to the websocket client
    on connect.
    """
    if asyncio.iscoroutinefunction(websocket.send_str):
        await websocket.send_str(json.dumps({"subject": Subject.websocket.value, "event": Event.connected.value}))
    else:
        websocket.send_str(json.dumps({"subject": Subject.websocket.value, "event": Event.connected.value}))

    logger.info("new authenticated connection, user: %s", websocket.cirrina.web_session.get("username"))


@app.websocket_message("/api/websocket")
async def websocket_message(websocket, msg):
    """
    On websocket message handler.
    """
    try:
        data = json.loads(msg)
    except json.decoder.JSONDecodeError:
        logger.error("cannot parse websocket message from user '%s'", websocket.cirrina.web_session.get("username"))

    await dispatch(websocket, data)


@app.websocket_disconnect()
async def websocket_closed(_):
    """
    On websocket disconnect handler.
    """
    logger.debug("websocket connection closed")
