// midi-test.swift — direct CoreMIDI test to send a note to mio
// Bypasses Python/rtmidi entirely. If Swift can play the JP but rtmidi can't,
// we've confirmed rtmidi is the problem.
//
//   swiftc midi-test.swift -o midi-test
//   ./midi-test list                 — list all destinations
//   ./midi-test send "mio" 60        — send C4 to destination matching "mio"

import Foundation
import CoreMIDI

func listDestinations() {
    let n = MIDIGetNumberOfDestinations()
    print("CoreMIDI destinations: \(n)")
    for i in 0..<n {
        let dest = MIDIGetDestination(i)
        var nameRef: Unmanaged<CFString>?
        MIDIObjectGetStringProperty(dest, kMIDIPropertyDisplayName, &nameRef)
        let displayName = nameRef?.takeRetainedValue() as String? ?? "?"
        var modelRef: Unmanaged<CFString>?
        MIDIObjectGetStringProperty(dest, kMIDIPropertyModel, &modelRef)
        let model = modelRef?.takeRetainedValue() as String? ?? "?"
        var uniqueID: Int32 = 0
        MIDIObjectGetIntegerProperty(dest, kMIDIPropertyUniqueID, &uniqueID)
        print("  [\(i)] '\(displayName)' (model='\(model)', uid=\(uniqueID))")
    }
}

func sendNote(toName name: String, midiNote: UInt8) {
    var client = MIDIClientRef()
    var status = MIDIClientCreate("spacepit-bench-test" as CFString, nil, nil, &client)
    if status != noErr { print("MIDIClientCreate err \(status)"); return }

    var outPort = MIDIPortRef()
    status = MIDIOutputPortCreate(client, "spacepit-out" as CFString, &outPort)
    if status != noErr { print("MIDIOutputPortCreate err \(status)"); return }

    // Find destination by name substring
    var target: MIDIEndpointRef = 0
    let n = MIDIGetNumberOfDestinations()
    for i in 0..<n {
        let dest = MIDIGetDestination(i)
        var nameRef: Unmanaged<CFString>?
        MIDIObjectGetStringProperty(dest, kMIDIPropertyDisplayName, &nameRef)
        let dn = (nameRef?.takeRetainedValue() as String? ?? "").lowercased()
        if dn.contains(name.lowercased()) {
            target = dest
            print("matched destination [\(i)] '\(dn)'")
            break
        }
    }
    if target == 0 { print("no destination matching '\(name)'"); return }

    // Build a packet list with note_on then 1.5s later note_off
    var packetList = MIDIPacketList()
    var packet = MIDIPacketListInit(&packetList)
    let noteOn: [UInt8] = [0x90, midiNote, 127]
    packet = MIDIPacketListAdd(&packetList, MemoryLayout<MIDIPacketList>.size,
                                packet, 0, noteOn.count, noteOn)
    status = MIDISend(outPort, target, &packetList)
    print("note_on send status: \(status)")

    // Sleep then send note_off in a separate packet list
    Thread.sleep(forTimeInterval: 1.5)

    var packetList2 = MIDIPacketList()
    var packet2 = MIDIPacketListInit(&packetList2)
    let noteOff: [UInt8] = [0x80, midiNote, 0]
    packet2 = MIDIPacketListAdd(&packetList2, MemoryLayout<MIDIPacketList>.size,
                                 packet2, 0, noteOff.count, noteOff)
    status = MIDISend(outPort, target, &packetList2)
    print("note_off send status: \(status)")

    MIDIPortDispose(outPort)
    MIDIClientDispose(client)
}

let args = CommandLine.arguments
if args.count < 2 || args[1] == "list" {
    listDestinations()
} else if args[1] == "send" && args.count >= 4 {
    let name = args[2]
    let note = UInt8(args[3]) ?? 60
    sendNote(toName: name, midiNote: note)
}
