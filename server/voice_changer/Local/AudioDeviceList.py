import sounddevice as sd
from dataclasses import dataclass, field

import numpy as np

from const import ServerAudioDeviceType
import logging

# from const import SERVER_DEVICE_SAMPLE_RATES

logger = logging.getLogger(__name__)


@dataclass
class ServerAudioDevice:
    index: int = 0
    name: str = ""
    hostAPI: str = ""
    maxInputChannels: int = 0
    maxOutputChannels: int = 0
    default_samplerate: int = 0
    # available_samplerates: list[int] = field(default_factory=lambda: [])


def dummy_callback(data: np.ndarray, frames, times, status):
    pass


def checkSamplingRate(deviceId: int, desiredSamplingRate: int, type: ServerAudioDeviceType):
    if type == "input":
        try:
            with sd.InputStream(
                device=deviceId,
                callback=dummy_callback,
                dtype="float32",
                samplerate=desiredSamplingRate,
            ):
                pass
            return True
        except Exception as e:  # NOQA
            err_str = str(e)
            logger.warning(f"[checkSamplingRate] Error opening InputStream: {err_str}")
            # WDM-KS devices often return paInvalidDevice (-9996) when probed via a
            # temporary stream (the host API can't be opened that way), but the device
            # may still be valid.  Trust the device's reported default_samplerate so
            # the actual stream-open attempt can report a real error instead of a
            # misleading "sample rate not supported" message.
            if 'Invalid device' in err_str or '-9996' in err_str:
                try:
                    dev_info = sd.query_devices(deviceId)
                    if abs(float(dev_info['default_samplerate']) - float(desiredSamplingRate)) < 1.0:
                        logger.debug(f"[checkSamplingRate] Device {deviceId} paInvalidDevice on probe; trusting default_samplerate={desiredSamplingRate}")
                        return True
                except Exception:
                    pass
            return False
    else:
        try:
            with sd.OutputStream(
                device=deviceId,
                callback=dummy_callback,
                dtype="float32",
                samplerate=desiredSamplingRate,
            ):
                pass
            return True
        except Exception as e:  # NOQA
            err_str = str(e)
            logger.warning(f"[checkSamplingRate] Error opening OutputStream: {err_str}")
            if 'Invalid device' in err_str or '-9996' in err_str:
                try:
                    dev_info = sd.query_devices(deviceId)
                    if abs(float(dev_info['default_samplerate']) - float(desiredSamplingRate)) < 1.0:
                        logger.debug(f"[checkSamplingRate] Device {deviceId} paInvalidDevice on probe; trusting default_samplerate={desiredSamplingRate}")
                        return True
                except Exception:
                    pass
            return False


def list_audio_device():
    try:
        audioDeviceList = sd.query_devices()
    except Exception as e:
        logger.exception(e)
        raise e

    inputAudioDeviceList = [d for d in audioDeviceList if d["max_input_channels"] > 0]
    outputAudioDeviceList = [d for d in audioDeviceList if d["max_output_channels"] > 0]
    hostapis = sd.query_hostapis()

    serverAudioInputDevices: list[ServerAudioDevice] = []
    serverAudioOutputDevices: list[ServerAudioDevice] = []
    for d in inputAudioDeviceList:
        serverInputAudioDevice: ServerAudioDevice = ServerAudioDevice(
            index=d["index"],
            name=d["name"],
            hostAPI=hostapis[d["hostapi"]]["name"],
            maxInputChannels=d["max_input_channels"],
            maxOutputChannels=d["max_output_channels"],
            default_samplerate=d["default_samplerate"],
        )
        serverAudioInputDevices.append(serverInputAudioDevice)
    for d in outputAudioDeviceList:
        serverOutputAudioDevice: ServerAudioDevice = ServerAudioDevice(
            index=d["index"],
            name=d["name"],
            hostAPI=hostapis[d["hostapi"]]["name"],
            maxInputChannels=d["max_input_channels"],
            maxOutputChannels=d["max_output_channels"],
            default_samplerate=d["default_samplerate"],
        )
        serverAudioOutputDevices.append(serverOutputAudioDevice)

    return serverAudioInputDevices, serverAudioOutputDevices


def resolve_device_index_by_name(name: str, device_type: ServerAudioDeviceType) -> int | None:
    """After sd._initialize(), resolve the current index for a device by its name.
    Returns the new index or None if not found."""
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    for d in devices:
        if d["name"] == name:
            if device_type == "input" and d["max_input_channels"] > 0:
                return d["index"]
            elif device_type == "output" and d["max_output_channels"] > 0:
                return d["index"]
    return None
