// HUMAN-MAINTAINED — do not modify via Claude Code without explicit human request.

import Foundation
import ScreenCaptureKit
import CoreImage
import AVFoundation
import CoreMedia

// MARK: - Constants

let VERSION = "ScreenCaptureHelper 0.1.0"
let PACKET_TYPE_AUDIO: UInt8 = 0x01
let PACKET_TYPE_VIDEO: UInt8 = 0x02
let TARGET_SAMPLE_RATE: Double = 16000
let TARGET_CHANNELS: UInt32 = 1
let JPEG_QUALITY: CGFloat = 0.85
let VIDEO_WIDTH = 1920
let VIDEO_HEIGHT = 1080

// MARK: - Globals

/// Disable stdout buffering so binary frames aren't held in libc buffers.
func disableStdoutBuffering() {
    setvbuf(stdout, nil, _IONBF, 0)
}

/// Wall-clock reference captured at startup for converting CMTime to UTC.
let startupWallTime = Date().timeIntervalSince1970
let startupMachTime = CACurrentMediaTime()

/// Convert a CMTime presentation timestamp to UTC epoch seconds.
func cmTimeToUTC(_ cmTime: CMTime) -> Double {
    let machSeconds = cmTime.seconds
    // CMTime from SCK uses the host time clock (mach time).
    // Convert to wall-clock UTC: wallTime + (sampleMach - startupMach)
    return startupWallTime + (machSeconds - startupMachTime)
}

// MARK: - Stderr logging

func ack(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

func ackOK(_ command: String) {
    ack("OK \(command)")
}

func ackERR(_ reason: String, detail: String? = nil) {
    if let detail = detail {
        ack("ERR \(reason) \(detail)")
    } else {
        ack("ERR \(reason)")
    }
}

// MARK: - Stdout packet writing

/// Write a framed packet to stdout.
/// Layout: [1 byte type][8 bytes float64 LE timestamp][4 bytes uint32 LE length][payload]
func writePacket(type packetType: UInt8, timestamp: Double, payload: Data) {
    var header = Data(capacity: 13)
    header.append(packetType)

    var ts = timestamp
    header.append(Data(bytes: &ts, count: 8)) // float64 LE (native on both arm64 and x86_64 macOS)

    var length = UInt32(payload.count)
    header.append(Data(bytes: &length, count: 4)) // uint32 LE

    // Write header + payload atomically (stdout is unbuffered)
    FileHandle.standardOutput.write(header)
    FileHandle.standardOutput.write(payload)
}

// MARK: - Stream Output Handler

class CaptureHandler: NSObject, SCStreamOutput {
    var audioEnabled = false
    var videoEnabled = false

    private let ciContext = CIContext()
    private let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!

    // Audio resampling
    private var audioConverter: AVAudioConverter?
    private var outputFormat: AVAudioFormat

    override init() {
        self.outputFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: TARGET_SAMPLE_RATE,
            channels: AVAudioChannelCount(TARGET_CHANNELS),
            interleaved: true
        )!
        super.init()
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        let cmTime = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        let utcTimestamp = cmTimeToUTC(cmTime)

        switch type {
        case .audio, .microphone:
            guard audioEnabled else { return }
            handleAudio(sampleBuffer: sampleBuffer, timestamp: utcTimestamp)
        case .screen:
            guard videoEnabled else { return }
            handleVideo(sampleBuffer: sampleBuffer, timestamp: utcTimestamp)
        @unknown default:
            break
        }
    }

    // MARK: Audio processing

    private func handleAudio(sampleBuffer: CMSampleBuffer, timestamp: Double) {
        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else {
            return
        }

        // Get the audio buffer list from the sample buffer
        var blockBuffer: CMBlockBuffer?
        var audioBufferList = AudioBufferList()
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: &audioBufferList,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: &blockBuffer
        )
        guard status == noErr else { return }

        let inputSampleRate = asbd.pointee.mSampleRate
        let inputChannels = asbd.pointee.mChannelsPerFrame
        let frameCount = CMSampleBufferGetNumSamples(sampleBuffer)

        // Create input format matching SCK's delivery format
        guard let inputFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: inputSampleRate,
            channels: AVAudioChannelCount(inputChannels),
            interleaved: false
        ) else { return }

        // Setup converter if needed (or if format changed)
        if audioConverter == nil || audioConverter!.inputFormat != inputFormat {
            audioConverter = AVAudioConverter(from: inputFormat, to: outputFormat)
            if audioConverter == nil {
                ackERR("audio_converter_init_failed")
                return
            }
        }

        // Create input PCM buffer from the sample buffer
        guard let inputBuffer = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: AVAudioFrameCount(frameCount)) else {
            return
        }
        inputBuffer.frameLength = AVAudioFrameCount(frameCount)

        // Copy float32 data from the audio buffer into the AVAudioPCMBuffer
        let srcBuffer = audioBufferList.mBuffers
        if let srcData = srcBuffer.mData, let dstData = inputBuffer.floatChannelData?[0] {
            memcpy(dstData, srcData, Int(srcBuffer.mDataByteSize))
        }

        // Calculate output frame count based on sample rate ratio
        let ratio = TARGET_SAMPLE_RATE / inputSampleRate
        let outputFrameCount = AVAudioFrameCount(Double(frameCount) * ratio) + 1

        guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: outputFrameCount) else {
            return
        }

        // Convert
        var error: NSError?
        let convertStatus = audioConverter!.convert(to: outputBuffer, error: &error) { inNumPackets, outStatus in
            outStatus.pointee = .haveData
            return inputBuffer
        }

        guard convertStatus != .error else {
            if let error = error {
                ackERR("audio_conversion_failed", detail: error.localizedDescription)
            }
            return
        }

        // Extract int16 PCM data from output buffer
        guard let int16Data = outputBuffer.int16ChannelData else { return }
        let sampleCount = Int(outputBuffer.frameLength)
        let byteCount = sampleCount * MemoryLayout<Int16>.size
        let payload = Data(bytes: int16Data[0], count: byteCount)

        writePacket(type: PACKET_TYPE_AUDIO, timestamp: timestamp, payload: payload)
    }

    // MARK: Video processing

    private func handleVideo(sampleBuffer: CMSampleBuffer, timestamp: Double) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        let ciImage = CIImage(cvPixelBuffer: pixelBuffer)

        // Render to JPEG
        guard let jpegData = ciContext.jpegRepresentation(
            of: ciImage,
            colorSpace: colorSpace,
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: JPEG_QUALITY]
        ) else {
            return
        }

        writePacket(type: PACKET_TYPE_VIDEO, timestamp: timestamp, payload: jpegData)
    }
}

// MARK: - Error Handler

class CaptureErrorHandler: NSObject, SCStreamDelegate {
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        ackERR("stream_stopped", detail: error.localizedDescription)
        // Exit code 2 signals the Python side to restart us
        exit(2)
    }
}

// MARK: - Main Controller

class HelperController {
    private var scStream: SCStream?
    private var streamConfig: SCStreamConfiguration
    private let handler = CaptureHandler()
    private let errorHandler = CaptureErrorHandler()
    private var currentDisplay: SCDisplay?
    private var contentFilter: SCContentFilter?
    private var streamStarted = false
    private let audioQueue = DispatchQueue(label: "com.helios.audio", qos: .userInteractive)
    private let videoQueue = DispatchQueue(label: "com.helios.video", qos: .userInteractive)

    init() {
        streamConfig = SCStreamConfiguration()
        streamConfig.width = VIDEO_WIDTH
        streamConfig.height = VIDEO_HEIGHT
        streamConfig.pixelFormat = kCVPixelFormatType_32BGRA
        streamConfig.minimumFrameInterval = CMTime(seconds: 1, preferredTimescale: 600)
        streamConfig.showsCursor = false
        streamConfig.capturesAudio = true
        streamConfig.sampleRate = 48000  // SCK native; we resample to 16 kHz in the handler
        streamConfig.channelCount = 1
    }

    // MARK: Command processing

    func processCommand(_ line: String) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        let parts = trimmed.split(separator: " ", maxSplits: 1)
        let command = String(parts[0])

        switch command {
        case "ENABLE_AUDIO":
            enableAudio()
        case "DISABLE_AUDIO":
            disableAudio()
        case "ENABLE_VIDEO":
            enableVideo()
        case "DISABLE_VIDEO":
            disableVideo()
        case "SET_DISPLAY":
            if parts.count > 1, let displayID = UInt32(parts[1]) {
                setDisplay(displayID)
            } else {
                ackERR("invalid_display_id", detail: parts.count > 1 ? String(parts[1]) : "missing")
            }
        case "QUIT":
            quit()
        default:
            ackERR("unknown_command", detail: command)
        }
    }

    // MARK: Audio control

    private func enableAudio() {
        ensureStreamStarted { [weak self] success in
            guard let self = self, success else {
                ackERR("stream_start_failed")
                return
            }
            self.handler.audioEnabled = true
            ackOK("ENABLE_AUDIO")
        }
    }

    private func disableAudio() {
        handler.audioEnabled = false
        ackOK("DISABLE_AUDIO")
    }

    // MARK: Video control

    private func enableVideo() {
        guard currentDisplay != nil else {
            ackERR("no_display_set", detail: "call SET_DISPLAY first")
            return
        }
        ensureStreamStarted { [weak self] success in
            guard let self = self, success else {
                ackERR("stream_start_failed")
                return
            }
            self.handler.videoEnabled = true
            ackOK("ENABLE_VIDEO")
        }
    }

    private func disableVideo() {
        handler.videoEnabled = false
        ackOK("DISABLE_VIDEO")
    }

    // MARK: Display management

    private func setDisplay(_ displayID: UInt32) {
        let semaphore = DispatchSemaphore(value: 0)
        var foundDisplay: SCDisplay?

        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { content, error in
            if let error = error {
                ackERR("shareable_content_failed", detail: error.localizedDescription)
                semaphore.signal()
                return
            }
            guard let content = content else {
                ackERR("shareable_content_nil")
                semaphore.signal()
                return
            }
            foundDisplay = content.displays.first { $0.displayID == displayID }
            if foundDisplay == nil {
                // If specific ID not found and we have displays, try first one
                if displayID == 0, let first = content.displays.first {
                    foundDisplay = first
                }
            }
            semaphore.signal()
        }
        semaphore.wait()

        if let display = foundDisplay {
            currentDisplay = display
            // Rebuild content filter and stream for the new display
            rebuildStream(for: display)
            ackOK("SET_DISPLAY \(displayID)")
        } else {
            ackERR("display_not_found", detail: "\(displayID)")
        }
    }

    private func rebuildStream(for display: SCDisplay) {
        // Stop existing stream if any
        if let existing = scStream {
            let semaphore = DispatchSemaphore(value: 0)
            existing.stopCapture { _ in semaphore.signal() }
            semaphore.wait()
            streamStarted = false
        }

        // Create content filter for the display (capture entire display, no excluded windows)
        contentFilter = SCContentFilter(display: display, excludingWindows: [])
        scStream = nil  // Will be created on next ensureStreamStarted
    }

    // MARK: Stream lifecycle

    private func ensureStreamStarted(completion: @escaping (Bool) -> Void) {
        if streamStarted {
            completion(true)
            return
        }

        // If no display set yet, get the main display
        if contentFilter == nil {
            let semaphore = DispatchSemaphore(value: 0)
            SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { [weak self] content, error in
                guard let self = self else { semaphore.signal(); return }
                if let display = content?.displays.first {
                    self.currentDisplay = display
                    self.contentFilter = SCContentFilter(display: display, excludingWindows: [])
                }
                semaphore.signal()
            }
            semaphore.wait()
        }

        guard let filter = contentFilter else {
            ackERR("no_display_available")
            completion(false)
            return
        }

        let stream = SCStream(filter: filter, configuration: streamConfig, delegate: errorHandler)
        self.scStream = stream

        do {
            try stream.addStreamOutput(handler, type: .audio, sampleHandlerQueue: audioQueue)
            try stream.addStreamOutput(handler, type: .screen, sampleHandlerQueue: videoQueue)
        } catch {
            ackERR("add_output_failed", detail: error.localizedDescription)
            completion(false)
            return
        }

        let semaphore = DispatchSemaphore(value: 0)
        var startError: Error?
        stream.startCapture { error in
            startError = error
            semaphore.signal()
        }
        semaphore.wait()

        if let error = startError {
            ackERR("stream_start_failed", detail: error.localizedDescription)
            completion(false)
        } else {
            streamStarted = true
            completion(true)
        }
    }

    // MARK: Shutdown

    private func quit() {
        handler.audioEnabled = false
        handler.videoEnabled = false

        if let stream = scStream {
            let semaphore = DispatchSemaphore(value: 0)
            stream.stopCapture { _ in semaphore.signal() }
            semaphore.wait()
        }

        ackOK("QUIT")
        exit(0)
    }
}

// MARK: - Entry point

disableStdoutBuffering()

// Handle --version flag before any SCK initialization
if CommandLine.arguments.contains("--version") {
    print(VERSION)
    exit(0)
}

let controller = HelperController()

// Read commands from stdin on a background thread
let stdinQueue = DispatchQueue(label: "com.helios.stdin")
stdinQueue.async {
    while let line = readLine(strippingNewline: false) {
        controller.processCommand(line)
    }
    // stdin closed — parent process died
    exit(0)
}

// Keep the main run loop alive for SCK callbacks
RunLoop.main.run()
