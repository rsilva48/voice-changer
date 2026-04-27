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
ERR_WASAPI_INIT_FAILED = (
    "WASAPI could not start (WDM-KS driver error returned by the audio device). "
    "Switch the input device to MME (e.g. 'Microphone (EPOS B20)') and try again."
)

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
        self.input_stream = None
        self.output_stream = None
        self.outQueue = Queue()

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

    # --- Split-stream callbacks (input device != output device) ---

    def audio_input_callback_split(self, indata: np.ndarray, frames, times, status):
        """Capture-only callback. Processes audio and queues output for the separate output stream."""
        try:
            out_wav = self._processDataWithTime(indata)
            try:
                self.outQueue.put_nowait(out_wav)
            except Exception:
                pass  # drop frame if queue is full
            try:
                self.monQueue.put_nowait(out_wav)
            except Exception:
                pass
        except Exception as e:
            self.callbacks.emit_to(0, self.performance, ('ERR_GENERIC_SERVER_AUDIO_ERROR', ERR_GENERIC_SERVER_AUDIO_ERROR))
            logger.exception(e)

    def audio_output_callback_split(self, outdata: np.ndarray, frames, times, status):
        """Playback-only callback. Reads processed audio from outQueue."""
        try:
            try:
                out_wav = self.outQueue.get(block=True, timeout=0.05)
            except Exception:
                outdata.fill(0)
                return
            while not self.outQueue.empty():
                try:
                    self.outQueue.get_nowait()
                except Exception:
                    break
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

    def _open_and_start_input_stream(self, **kwargs) -> sd.InputStream:
        """Create and start an InputStream.

        For WASAPI shared-mode streams: if PortAudio throws a WdmSyncIoctl /
        WDM-KS error during start() (e.g. KSPROPSETID_AudioSignalProcessing
        not found on USB devices like the EPOS B20), automatically retry in
        WASAPI *exclusive* mode.  Exclusive mode bypasses the Windows APO
        chain and therefore skips the offending property query.
        """
        extra_settings = kwargs.get('extra_settings')
        attempts: list[tuple] = [(extra_settings, 'configured')]
        if isinstance(extra_settings, sd.WasapiSettings) and not extra_settings.exclusive:
            attempts.append((sd.WasapiSettings(exclusive=True, auto_convert=False), 'WASAPI exclusive'))

        last_exc: sd.PortAudioError | None = None
        for settings, label in attempts:
            s = sd.InputStream(**{**kwargs, 'extra_settings': settings})
            try:
                s.start()
                if last_exc is not None:
                    logger.warning(f"[ServerAudio] Input stream started with {label} (shared WASAPI failed: {last_exc})")
                return s
            except sd.PortAudioError as e:
                s.close()
                if 'WdmSyncIoctl' in str(e) or 'WDM-KS error' in str(e):
                    last_exc = e
                    logger.warning(f"[ServerAudio] Input stream {label!r} failed (WdmSyncIoctl); trying next fallback")
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    def run_no_monitor(self, block_frame: int, inputMaxChannel: int, outputMaxChannel: int, inputExtraSetting, outputExtraSetting, inputDeviceId: int, outputDeviceId: int):
        if inputDeviceId == outputDeviceId:
            # Same device: use duplex stream (shared clock, no underruns)
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
        else:
            # Different devices: use separate input/output streams to avoid clock-sync crash
            self.input_stream = self._open_and_start_input_stream(
                callback=self.audio_input_callback_split,
                latency='low',
                dtype="float32",
                device=inputDeviceId,
                blocksize=block_frame,
                samplerate=self.settings.serverInputAudioSampleRate,
                channels=inputMaxChannel,
                extra_settings=inputExtraSetting,
            )
            self.output_stream = sd.OutputStream(
                callback=self.audio_output_callback_split,
                latency='low',
                dtype="float32",
                device=outputDeviceId,
                blocksize=block_frame,
                samplerate=self.settings.serverOutputAudioSampleRate,
                channels=outputMaxChannel,
                extra_settings=outputExtraSetting
            )
            self.output_stream.start()

    def run_with_monitor(self, block_frame: int, inputMaxChannel: int, outputMaxChannel: int, monitorMaxChannel: int, inputExtraSetting, outputExtraSetting, monitorExtraSetting, inputDeviceId: int, outputDeviceId: int, monitorDeviceId: int):
        if inputDeviceId == outputDeviceId:
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
            self.stream.start()
        else:
            self.input_stream = self._open_and_start_input_stream(
                callback=self.audio_input_callback_split,
                latency='low',
                dtype="float32",
                device=inputDeviceId,
                blocksize=block_frame,
                samplerate=self.settings.serverInputAudioSampleRate,
                channels=inputMaxChannel,
                extra_settings=inputExtraSetting,
            )
            self.output_stream = sd.OutputStream(
                callback=self.audio_output_callback_split,
                latency='low',
                dtype="float32",
                device=outputDeviceId,
                blocksize=block_frame,
                samplerate=self.settings.serverOutputAudioSampleRate,
                channels=outputMaxChannel,
                extra_settings=outputExtraSetting
            )
            self.output_stream.start()
        self.monitor = sd.OutputStream(
            callback=self.audio_monitor_callback,
            dtype="float32",
            device=monitorDeviceId,
            blocksize=block_frame,
            samplerate=self.settings.serverMonitorAudioSampleRate,
            channels=monitorMaxChannel,
            extra_settings=monitorExtraSetting
        )
        self.monitor.start()

    def stop(self):
        self.running = False
        # Drain queues so callbacks unblock immediately
        for q in (self.monQueue, self.outQueue):
            try:
                while True:
                    q.get_nowait()
            except Exception:
                pass
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        if self.input_stream is not None:
            self.input_stream.close()
            self.input_stream = None
        if self.output_stream is not None:
            self.output_stream.close()
            self.output_stream = None
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

        # Re-resolve by name so indices don't need to be updated after every restart.
        # Device names like 'pipewire' and 'pulse' are stable; only their ALSA index
        # shifts when new sources (e.g. RVC-Mic) are created by the startup script.
        def _resolve_by_name(name: str, by_index: dict) -> int | None:
            if not name:
                return None
            for dev in by_index.values():
                if dev.name == name:
                    return dev.index
            return None

        resolved = _resolve_by_name(self.settings.serverInputDeviceName, inputById)
        if resolved is not None:
            inputDeviceId = resolved
            self.settings.serverInputDeviceId = resolved  # keep UI in sync

        resolved = _resolve_by_name(self.settings.serverOutputDeviceName, outputById)
        if resolved is not None:
            outputDeviceId = resolved
            self.settings.serverOutputDeviceId = resolved  # keep UI in sync

        resolved = _resolve_by_name(self.settings.serverMonitorDeviceName, outputById)
        if resolved is not None:
            monitorDeviceId = resolved
            self.settings.serverMonitorDeviceId = resolved  # keep UI in sync

        serverInputAudioDevice = inputById.get(inputDeviceId)
        serverOutputAudioDevice = outputById.get(outputDeviceId)
        serverMonitorAudioDevice = outputById.get(monitorDeviceId) if monitorDeviceId >= 0 else None

        # Generate ExtraSetting
        wasapiExclusiveMode = bool(self.settings.exclusiveMode)

        # Cap to stereo — ALSA virtual devices (pipewire, pulse) report huge channel
        # counts (128, 32) but opening them with that many channels causes PipeWire to
        # use non-standard routing, resulting in glitchy / overlapping audio.
        inputChannels = min(serverInputAudioDevice.maxInputChannels, 2)
        inputExtraSetting = None
        if serverInputAudioDevice and "WASAPI" in serverInputAudioDevice.hostAPI:
            inputExtraSetting = sd.WasapiSettings(exclusive=wasapiExclusiveMode, auto_convert=not wasapiExclusiveMode)
            inputChannels = serverInputAudioDevice.maxInputChannels  # WASAPI: use device max
        elif serverInputAudioDevice and "ASIO" in serverInputAudioDevice.hostAPI and self.settings.asioInputChannel != -1:
            inputExtraSetting = sd.AsioSettings(channel_selectors=[self.settings.asioInputChannel])
            inputChannels = 1

        outputChannels = min(serverOutputAudioDevice.maxOutputChannels, 2)
        outputExtraSetting = None
        if serverOutputAudioDevice and "WASAPI" in serverOutputAudioDevice.hostAPI:
            outputExtraSetting = sd.WasapiSettings(exclusive=wasapiExclusiveMode, auto_convert=not wasapiExclusiveMode)
            outputChannels = serverOutputAudioDevice.maxOutputChannels  # WASAPI: use device max
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
            # Close any streams that were opened but not started before the exception.
            self.stop()
            # Reset flag so the REST response returns serverAudioStated=0 → UI toggle turns off.
            self.settings.serverAudioStated = 0
            # Provide a targeted message for the well-known WASAPI/WDM-KS driver error so the
            # user knows to switch to MME rather than seeing a cryptic PortAudio traceback.
            err_str = str(e)
            if 'WdmSyncIoctl' in err_str or 'WDM-KS error' in err_str:
                user_msg = ERR_WASAPI_INIT_FAILED
            else:
                user_msg = ERR_GENERIC_SERVER_AUDIO_ERROR
            self.callbacks.emit_to(0, self.performance, ('ERR_GENERIC_SERVER_AUDIO_ERROR', user_msg))
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
