"""Feed the RM's views to a client so it follows membership and the passive primary."""

from ft_system.common.protocol import MessageType, ProtocolError
from ft_system.common.rm_link import RmLink


def subscribe(client, rm_config, logger, identity=None):
    """Return an RmLink whose ``run()`` keeps ``client`` up to date with each view."""

    async def on_message(message):
        if message["type"] != MessageType.VIEW:
            raise ProtocolError(f"unexpected {message['type']} from RM")
        logger.view(client.client_id, message["payload"])
        client.apply_view(message["payload"])

    return RmLink(client.client_id, "client", rm_config, logger, on_message, identity=identity)
