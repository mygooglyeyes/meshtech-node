"""cleanmodem - a clean-room LoRa modem for the PiMesh-1W v2.

One process owns the SX1262 radio and serves the repeater's modem
wire protocol (observer clients) plus the bot's controller connection
(exclusive TX). See PROTOCOL.md for the wire format and README.md for
the architecture.
"""
__version__ = "0.1.0"
