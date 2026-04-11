#!/bin/bash

set -e

echo "==============================================="
echo "Voice Changer Server Startup Script"
echo "==============================================="
echo ""

# Module index of the RVC virtual microphone (set at runtime)
RVC_MOD_ID=""

# Function to check if virtual environment exists
check_venv() {
    if [ ! -d "venv" ]; then
        echo "Error: Virtual environment not found!"
        echo "Please run the installation script first:"
        echo "  ./install.sh"
        echo ""
        exit 1
    fi
    
    echo "Virtual environment found"
}

# Function to check if main.py exists
check_app() {
    if [ ! -f "main.py" ]; then
        echo "Error: main.py not found!"
        echo "Please make sure you're running this script from the server directory"
        echo ""
        exit 1
    fi
    
    echo "Application file found"
}

# Function to activate virtual environment
activate_venv() {
    echo "Activating virtual environment..."
    
    # Activate virtual environment
    if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
        source venv/Scripts/activate
    else
        source venv/bin/activate
    fi
    
    # Check if activation was successful
    if [ -z "$VIRTUAL_ENV" ]; then
        echo "Error: Failed to activate virtual environment"
        exit 1
    fi
    
    echo "Virtual environment activated: $VIRTUAL_ENV"
}

# Function to start the application
start_app() {
    echo ""
    echo "Starting Voice Changer Server..."
    echo "Press Ctrl+C to stop the server"
    echo ""
    
    # Input: pipewire  — captura del micrófono (EPOS B20 vía sistema)
    # Output: pulse    — enruta a snd-aloop loopback (streams SEPARADOS, sin crash de reloj)
    # Requires: sudo modprobe snd-aloop  (permanent: /etc/modules-load.d/snd-aloop.conf)
    export PULSE_SINK=alsa_output.platform-snd_aloop.0.analog-stereo
    export PIPEWIRE_ALSA='{"alsa.rate": 48000}'

    # Discord and OBS do not list PipeWire monitor sources in their device menus.
    # Wrap the loopback monitor in a proper named source so it appears as a real mic.
    # Unload any stale instance from a previous server run first.
    stale_id=$(pactl list short modules 2>/dev/null | awk '/RVC-Mic/{print $1; exit}') || true
    [ -n "${stale_id:-}" ] && pactl unload-module "$stale_id" 2>/dev/null || true
    RVC_MOD_ID=$(pactl load-module module-remap-source \
        source_name=RVC-Mic \
        master=alsa_output.platform-snd_aloop.0.analog-stereo.monitor \
        source_properties=device.description=RVC-Microphone 2>/dev/null) || true

    # Suppress MIOpen/ROCm workspace tuning warnings (normal on first inference)
    export MIOPEN_LOG_LEVEL=2
    export AMD_LOG_LEVEL=0
    
    # Start the application
    python main.py

}

# Function to handle cleanup on exit
cleanup() {
    echo ""
    echo "Shutting down Voice Changer Server..."
    # Remove the virtual microphone source created at startup
    [ -n "${RVC_MOD_ID:-}" ] && pactl unload-module "$RVC_MOD_ID" 2>/dev/null || true
    echo "Goodbye!"
}

# Set trap for cleanup on exit
trap cleanup EXIT

# Main startup process
main() {
    echo "Starting Voice Changer Server..."
    echo ""
    
    # Check if we're in the server directory
    if [ ! -f "main.py" ]; then
        echo "Error: This script must be run from the server directory"
        echo "Please navigate to the server directory and run the script again"
        exit 1
    fi
    
    check_venv
    check_app
    activate_venv
    start_app
}

# Run main function
main