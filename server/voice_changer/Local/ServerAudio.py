import numpy as np
from const import SERVER_DEVICE_SAMPLE_RATES

from queue import Queue
import logging
from voice_changer.VoiceChangerSettings import VoiceChangerSettings
from voice_changer.Local.AudioDeviceList import checkSamplingRate, list_audio_device, resolve_device_index_by_name
import sounddevice as sd
import librosa

from voice_changer.utils.VoiceChangerModel import AudioInOutFloat
from typing import Union, Protocol

logger = logging.getLogger(__name__)

ERR_SAMPLE_RATE_NOT_SUPPORTED = """Specified sample rate is not supported by all selected audio devices.
Available sample rates:
  [Input]: %s
  [Output]: %s
  [Monitor]: %s"""
ERR_GENERIC_SERVER_AUDIO_ERROR = "A server audio error occurred."

class ServerAudioCallbacks(Protocol):
    def on_audio(self, unpackedData: AudioInOutFloat) -> tuple[AudioInOutFloat, list[Union[int, float]]]:
        ...

    def emit_to(self, volume: float, performance: list[float], err: tuple[str, str] | None):
        ...

class ServerAudio:
    def __init__(self, callbacks: ServerAudioCallbacks, settings: VoiceChangerSettings):
        self.settings = settings
        self.callbacks = callbacks
        self.mon_wav = None
        self.serverAudioInputDevices = None
        self.serverAudioOutputDevices = None
        self.monQueue = Queue()
        self.performance = [0, 0, 0]

        self.stream = None
        self.monitor = None

        self.running = False

    def getServerInputAudioDevice(self, index: int):
        audioinput, _ = list_audio_device()
        serverAudioDevice = [x for x in audioinput if x.index == index]
        if len(serverAudioDevice) > 0:
            return serverAudioDevice[0]
        else:
            return None

    def getServerOutputAudioDevice(self, index: int):
        _, audiooutput = list_audio_device()
        serverAudioDevice = [x for x in audiooutput if x.index == index]
        if len(serverAudioDevice) > 0:
            return serverAudioDevice[0]
        else:
            return None

    ###########################################
    # Callback Section
    ###########################################

    def _processData(self, indata: np.ndarray):
        indata = indata * self.settings.serverInputAudioGain
        unpackedData = librosa.to_mono(indata.T)
        return self.callbacks.on_audio(unpackedData)

    def _processDataWithTime(self, indata: np.ndarray):
        out_wav, vol, perf, err = self._processData(indata)
        self.performance = perf
        self.callbacks.emit_to(vol, self.performance, err)
        return out_wav

    def audio_stream_callback(self, indata: np.ndarray, outdata: np.ndarray, frames, times, status):
        try:
            out_wav = self._processDataWithTime(indata)
            outputChannels = outdata.shape[1]
            outdata[:] = (np.repeat(out_wav, outputChannels).reshape(-1, outputChannels) * self.settings.serverOutputAudioGain)
        except Exception as e:
            self.callbacks.emit_to(0, self.performance, ('ERR_GENERIC_SERVER_AUDIO_ERROR', ERR_GENERIC_SERVER_AUDIO_ERROR))
            logger.exception(e)

    def audio_stream_callback_mon_queue(self, indata: np.ndarray, outdata: np.ndarray, frames, times, status):
        try:
            out_wav = self._processDataWithTime(indata)
            self.monQueue.put(out_wav)
            outputChannels = outdata.shape[1]
            outdata[:] = (np.repeat(out_wav, outputChannels).reshape(-1, outputChannels) * self.settings.serverOutputAudioGain)
        except Exception as e:
            self.callbacks.emit_to(0, self.performance, ('ERR_GENERIC_SERVER_AUDIO_ERROR', ERR_GENERIC_SERVER_AUDIO_ERROR))
            logger.exception(e)

    def audio_monitor_callback(self, outdata: np.ndarray, frames, times, status):
        try:
            try:
                mon_wav = self.monQueue.get(block=True, timeout=0.05)
            except Exception:
                outdata.fill(0)
                return
            # Drain stale frames so monitor stays in sync
            while not self.monQueue.empty():
                try:
                    self.monQueue.get_nowait()
                except Exception:
                    break
            outputChannels = outdata.shape[1]
            outdata[:] = (np.repeat(mon_wav, outputChannels).reshape(-1, outputChannels) * self.settings.serverMonitorAudioGain)
        except Exception as e:
            self.callbacks.emit_to(0, self.performance, ('ERR_GENERIC_SERVER_AUDIO_ERROR', ERR_GENERIC_SERVER_AUDIO_ERROR))
            logger.exception(e)

    ###########################################
    # Main Loop Section
    ###########################################
    def run_no_monitor(self, block_frame: int, inputMaxChannel: int, outputMaxChannel: int, inputExtraSetting, outputExtraSetting, inputDeviceId: int, outputDeviceId: int):
        self.stream = sd.Stream(
            callback=self.audio_stream_callback,
            latency='low',
            dtype="float32",
            device=(inputDeviceId, outputDeviceId),
            blocksize=block_frame,
            samplerate=self.settings.serverInputAudioSampleRate,
            channels=(inputMaxChannel, outputMaxChannel),
            extra_settings=(inputExtraSetting, outputExtraSetting)
        )
        self.stream.start()

    def run_with_monitor(self, block_frame: int, inputMaxChannel: int, outputMaxChannel: int, monitorMaxChannel: int, inputExtraSetting, outputExtraSetting, monitorExtraSetting, inputDeviceId: int, outputDeviceId: int, monitorDeviceId: int):
        self.stream = sd.Stream(
            callback=self.audio_stream_callback_mon_queue,
            latency='low',
            dtype="float32",
            device=(inputDeviceId, outputDeviceId),
            blocksize=block_frame,
            samplerate=self.settings.serverInputAudioSampleRate,
            channels=(inputMaxChannel, outputMaxChannel),
            extra_settings=(inputExtraSetting, outputExtraSetting)
        )
        self.monitor = sd.OutputStream(
            callback=self.audio_monitor_callback,
            dtype="float32",
            device=monitorDeviceId,
            blocksize=block_frame,
            samplerate=self.settings.serverMonitorAudioSampleRate,
            channels=monitorMaxChannel,
            extra_settings=monitorExtraSetting
        )
        self.stream.start()
        self.monitor.start()

    def stop(self):
        self.running = False
        # Drain monQueue so audio_monitor_callback unblocks immediately
        try:
            while True:
                self.monQueue.get_nowait()
        except Exception:
            pass
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        if self.monitor is not None:
            self.monitor.close()
            self.monitor = None

    ###########################################
    # Start Section
    ###########################################

    # ALSA device names that bypass PipeWire and access hardware directly.
    # Opening these while PipeWire has exclusive control causes SIGABRT.
    _UNSAFE_ALSA_DEVICES = {'default', 'null'}

    def _is_safe_device(self, device: 'ServerAudioDevice') -> bool:
        """Return False for devices that go to hw directly and crash under PipeWire."""
        name = device.name.lower()
        if name in self._UNSAFE_ALSA_DEVICES:
            return False
        if name.startswith('hw:') or name.startswith('plughw:'):
            return False
        return True

    def start(self):
        self.stop()

        # NOTE: sd._terminate()/_initialize() was removed intentionally.
        # It caused ALSA to renumber device indices (e.g. 'pulse'↔'default' swap)
        # making the saved index point to the wrong device on every restart.
        # Device indices on Linux are stable while hardware doesn't change.
        inputDevices, outputDevices = list_audio_device()
        inputById = {d.index: d for d in inputDevices}
        outputById = {d.index: d for d in outputDevices}

        inputDeviceId = self.settings.serverInputDeviceId
        outputDeviceId = self.settings.serverOutputDeviceId
        monitorDeviceId = self.settings.serverMonitorDeviceId

        serverInputAudioDevice = inputById.get(inputDeviceId)
        serverOutputAudioDevice = outputById.get(outputDeviceId)
        serverMonitorAudioDevice = outputById.get(monitorDeviceId) if monitorDeviceId >= 0 else None

        # Generate ExtraSetting
        wasapiExclusiveMode = bool(self.settings.exclusiveMode)

        inputChannels = serverInputAudioDevice.maxInputChannels
        inputExtraSetting = None
        if serverInputAudioDevice and "WASAPI" in serverInputAudioDevice.hostAPI:
            inputExtraSetting = sd.WasapiSettings(exclusive=wasapiExclusiveMode, auto_convert=not wasapiExclusiveMode)
        elif serverInputAudioDevice and "ASIO" in serverInputAudioDevice.hostAPI and self.settings.asioInputChannel != -1:
            inputExtraSetting = sd.AsioSettings(channel_selectors=[self.settings.asioInputChannel])
            inputChannels = 1

        outputChannels = serverOutputAudioDevice.maxOutputChannels
        outputExtraSetting = None
        if serverOutputAudioDevice and "WASAPI" in serverOutputAudioDevice.hostAPI:
            outputExtraSetting = sd.WasapiSettings(exclusive=wasapiExclusiveMode, auto_convert=not wasapiExclusiveMode)
        elif serverInputAudioDevice and "ASIO" in serverInputAudioDevice.hostAPI and self.settings.asioOutputChannel != -1:
            outputExtraSetting = sd.AsioSettings(channel_selectors=[self.settings.asioOutputChannel])
            outputChannels = 1

        monitorExtraSetting = None
        if serverMonitorAudioDevice and "WASAPI" in serverMonitorAudioDevice.hostAPI:
            monitorExtraSetting = sd.WasapiSettings(exclusive=wasapiExclusiveMode, auto_convert=not wasapiExclusiveMode)

        logger.info("Devices:")
        logger.info(f"  [Input]: {serverInputAudioDevice} {inputExtraSetting}")
        logger.info(f"  [Output]: {serverOutputAudioDevice}, {outputExtraSetting}")
        logger.info(f"  [Monitor]: {serverMonitorAudioDevice}, {monitorExtraSetting}")

        # Early null check — avoids AttributeError below if device lookup failed
        if serverInputAudioDevice is None or serverOutputAudioDevice is None:
            logger.error("Input or output device is not selected.")
            self.callbacks.emit_to(0, self.performance, ('ERR_GENERIC_SERVER_AUDIO_ERROR', ERR_GENERIC_SERVER_AUDIO_ERROR))
            return

        # Refuse to open devices that bypass PipeWire and go to hw directly (causes SIGABRT)
        for dev, label in [(serverInputAudioDevice, 'Input'), (serverOutputAudioDevice, 'Output')]:
            if not self._is_safe_device(dev):
                msg = f"{label} device '{dev.name}' accesses hardware directly and will crash under PipeWire. Use 'pipewire' or 'pulse' instead."
                logger.error(msg)
                self.callbacks.emit_to(0, self.performance, ('ERR_GENERIC_SERVER_AUDIO_ERROR', msg))
                return

        # サンプリングレート
        # 同一サンプリングレートに統一（変換時にサンプルが不足する場合があるため。パディング方法が明らかになれば、それぞれ設定できるかも）
        self.settings.serverInputAudioSampleRate = self.settings.serverAudioSampleRate
        self.settings.serverOutputAudioSampleRate = self.settings.serverAudioSampleRate
        self.settings.serverMonitorAudioSampleRate = self.settings.serverAudioSampleRate

        # Sample Rate Check
        if "WASAPI" not in serverInputAudioDevice.hostAPI and not wasapiExclusiveMode:
            inputAudioSampleRateAvailable = checkSamplingRate(inputDeviceId, self.settings.serverInputAudioSampleRate, "input")
            outputAudioSampleRateAvailable = checkSamplingRate(outputDeviceId, self.settings.serverOutputAudioSampleRate, "output")
            monitorAudioSampleRateAvailable = checkSamplingRate(monitorDeviceId, self.settings.serverMonitorAudioSampleRate, "output") if serverMonitorAudioDevice else True

            logger.info("Sample Rate:")
            logger.info(f"  [Input]: {self.settings.serverInputAudioSampleRate} -> {inputAudioSampleRateAvailable}")
            logger.info(f"  [Output]: {self.settings.serverOutputAudioSampleRate} -> {outputAudioSampleRateAvailable}")
            if serverMonitorAudioDevice is not None:
                logger.info(f"  [Monitor]: {self.settings.serverMonitorAudioSampleRate} -> {monitorAudioSampleRateAvailable}")

            if not inputAudioSampleRateAvailable or not outputAudioSampleRateAvailable or not monitorAudioSampleRateAvailable:
                logger.info("Checking Available Sample Rate:")
                availableInputSampleRate = []
                availableOutputSampleRate = []
                availableMonitorSampleRate = []
                for sr in SERVER_DEVICE_SAMPLE_RATES:
                    if checkSamplingRate(inputDeviceId, sr, "input"):
                        availableInputSampleRate.append(sr)
                    if checkSamplingRate(outputDeviceId, sr, "output"):
                        availableOutputSampleRate.append(sr)
                    if serverMonitorAudioDevice is not None:
                        if checkSamplingRate(monitorDeviceId, sr, "output"):
                            availableMonitorSampleRate.append(sr)
                err = ERR_SAMPLE_RATE_NOT_SUPPORTED % (availableInputSampleRate, availableOutputSampleRate, availableMonitorSampleRate)
                self.callbacks.emit_to(
                    0,
                    self.performance,
                    ('ERR_SAMPLE_RATE_NOT_SUPPORTED', err)
                )
                logger.error(err)
                return

        # FIXME: In UI, block size is calculated based on 48kHz so we convert from 48kHz to input device sample rate.
        block_frame = int((self.settings.serverReadChunkSize * 128 / 48000) * self.settings.serverInputAudioSampleRate)

        try:
            if serverMonitorAudioDevice is None:
                self.run_no_monitor(block_frame, inputChannels, outputChannels, inputExtraSetting, outputExtraSetting, inputDeviceId, outputDeviceId)
            else:
                self.run_with_monitor(block_frame, inputChannels, outputChannels, serverMonitorAudioDevice.maxOutputChannels, inputExtraSetting, outputExtraSetting, monitorExtraSetting, inputDeviceId, outputDeviceId, monitorDeviceId)
            self.running = True
        except Exception as e:
            self.callbacks.emit_to(0, self.performance, ('ERR_GENERIC_SERVER_AUDIO_ERROR', ERR_GENERIC_SERVER_AUDIO_ERROR))
            logger.exception(e)

    ###########################################
    # Info Section
    ###########################################
    def get_info(self):
        data = {}
        try:
            audioinput, audiooutput = list_audio_device()
            self.serverAudioInputDevices = audioinput
            self.serverAudioOutputDevices = audiooutput
        except Exception as e:
            self.callbacks.emit_to(0, self.performance, ('ERR_GENERIC_SERVER_AUDIO_ERROR', ERR_GENERIC_SERVER_AUDIO_ERROR))
            logger.exception(e)

        data["serverAudioInputDevices"] = self.serverAudioInputDevices
        data["serverAudioOutputDevices"] = self.serverAudioOutputDevices
        return data

    def update_settings(self, key: str, val, old_val):
        if key == 'serverAudioStated':
            if val:
                self.start()
            else:
                self.stop()
        if self.running and key in { 'serverInputDeviceId', 'serverOutputDeviceId', 'serverMonitorDeviceId', 'serverReadChunkSize', 'serverAudioSampleRate', 'asioInputChannel', 'asioOutputChannel' }:
            self.start()
