"""ROS-independent, versioned RGB-D grasp contract.

Frames use T_A_B to map points from B into A. A candidate's origin is the
closing/contact center, X is the closing axis and Z is positive approach.
No wrist offset, TCP or robot name belongs in this contract.
"""

from .schema import (
    IDENTITY_FIELDS,
    PROTOCOL_VERSION,
    ProtocolError,
    identity,
    validate_candidate,
    validate_request,
    validate_response,
)
from .wire import MAX_MESSAGE_BYTES, packb, unpackb

__all__ = [
    "IDENTITY_FIELDS", "MAX_MESSAGE_BYTES", "PROTOCOL_VERSION", "ProtocolError",
    "identity", "packb", "unpackb", "validate_candidate", "validate_request",
    "validate_response",
]
