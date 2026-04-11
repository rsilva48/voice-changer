import sounddevice as sd
for d in sd.query_devices():
    print(f"[{d['index']}] {d['name']} in={d['max_input_channels']} out={d['max_output_channels']}")
