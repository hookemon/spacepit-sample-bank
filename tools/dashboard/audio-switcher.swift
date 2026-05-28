// audio-switcher.swift — tiny macOS CLI that lists output devices + switches the default.
// Built once with `swiftc audio-switcher.swift -o audio-switcher`.
// Replaces switchaudio-osx for the bench so no brew install needed.
//
// Usage:
//   ./audio-switcher list             — print all output devices, one per line as "id|name|is_default"
//   ./audio-switcher set "TX-6"       — set default output by name substring (case-insensitive)
//   ./audio-switcher current          — print just the current default output name

import Foundation
import CoreAudio

func getAllDeviceIDs() -> [AudioDeviceID] {
    var size: UInt32 = 0
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    let n = Int(size) / MemoryLayout<AudioDeviceID>.size
    var ids = [AudioDeviceID](repeating: 0, count: n)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
    return ids
}

func deviceName(_ id: AudioDeviceID) -> String {
    var name: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceNameCFString,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &name)
    return name as String
}

func hasOutputChannels(_ id: AudioDeviceID) -> Bool {
    // Check whether the device has any output streams. Simpler than parsing
    // the AudioBufferList — just ask Core Audio how many output streams exist.
    var size: UInt32 = 0
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreams,
        mScope: kAudioDevicePropertyScopeOutput,
        mElement: kAudioObjectPropertyElementMain
    )
    AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size)
    return size > 0
}

func currentDefaultOutput() -> AudioDeviceID {
    var id: AudioDeviceID = 0
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &id)
    return id
}

func setDefaultOutput(_ id: AudioDeviceID) -> OSStatus {
    var deviceID = id
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    return AudioObjectSetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil,
        UInt32(MemoryLayout<AudioDeviceID>.size), &deviceID
    )
}

// ---- main ----
let args = CommandLine.arguments
let command = args.count > 1 ? args[1] : "list"

let defaultID = currentDefaultOutput()
let allOutputs = getAllDeviceIDs().filter { hasOutputChannels($0) }

switch command {
case "list":
    for id in allOutputs {
        let name = deviceName(id)
        let mark = (id == defaultID) ? "default" : ""
        print("\(id)|\(name)|\(mark)")
    }
case "current":
    print(deviceName(defaultID))
case "set":
    if args.count < 3 {
        FileHandle.standardError.write("error: missing device name\n".data(using: .utf8)!)
        exit(1)
    }
    let target = args[2].lowercased()
    if let match = allOutputs.first(where: { deviceName($0).lowercased().contains(target) }) {
        let err = setDefaultOutput(match)
        if err == 0 {
            print("ok: switched to \(deviceName(match))")
        } else {
            FileHandle.standardError.write("error: Core Audio returned \(err)\n".data(using: .utf8)!)
            exit(2)
        }
    } else {
        FileHandle.standardError.write("error: no output device matching '\(args[2])'\n".data(using: .utf8)!)
        exit(3)
    }
default:
    FileHandle.standardError.write("usage: audio-switcher [list|current|set NAME]\n".data(using: .utf8)!)
    exit(1)
}
