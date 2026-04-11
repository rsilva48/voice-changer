import msgspec
from socketio import packet
from socketio.packet import Packet as _DefaultPacket


class MsgPackPacket(packet.Packet):
    uses_binary_events = False

    def encode(self):
        """Encode the packet for transmission."""
        return msgspec.msgpack.encode(self._to_dict())

    def decode(self, encoded_packet):
        """Decode a transmitted package."""
        if isinstance(encoded_packet, str):
            # Plain text socket.io packet (e.g. connect/disconnect handshake).
            # Delegate to the default text-based decoder.
            default = _DefaultPacket(encoded_packet=encoded_packet)
            self.packet_type = default.packet_type
            self.data = default.data
            self.id = default.id
            self.namespace = default.namespace
            return
        try:
            decoded = msgspec.msgpack.decode(encoded_packet)
        except msgspec.DecodeError:
            # Fallback: treat as default packet if msgpack decoding fails
            default = _DefaultPacket(encoded_packet=encoded_packet)
            self.packet_type = default.packet_type
            self.data = default.data
            self.id = default.id
            self.namespace = default.namespace
            return
        self.packet_type = decoded['type']
        self.data = decoded.get('data')
        self.id = decoded.get('id')
        self.namespace = decoded['nsp']
