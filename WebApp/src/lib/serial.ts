// Reads the firmware's UART stream through the Web Serial API.
//
// Firmware/src/main.c prints one line per sample, ">CH1:<mV>" with exactly
// three decimals, at 500 SPS and 115200 baud. Zephyr log lines share the port.

export const BAUD_RATE = 115200
// The nRF5340 DK's UART reaches USB through its on-board SEGGER J-Link.
const SEGGER_VENDOR_ID = 0x1366

const SAMPLE_LINE = /^>CH1:(-?\d+\.\d{3})$/

/** Millivolts from one line, or null for log output and damaged lines. */
export function parseLine(line: string): number | null {
  const match = SAMPLE_LINE.exec(line)
  return match ? Number(match[1]) : null
}

/** Turns arbitrary chunks of serial text into samples, holding back the
 *  unfinished last line until the rest of it arrives. */
export class SampleParser {
  private partial = ''

  push(text: string): number[] {
    const lines = (this.partial + text).split('\n')
    this.partial = lines.pop() ?? ''
    const samples: number[] = []
    for (const line of lines) {
      const mv = parseLine(line.replace(/\r$/, ''))
      if (mv !== null) samples.push(mv)
    }
    return samples
  }
}

export interface SampleSource {
  /** `onEnd` fires only if the stream stops on its own (cable pulled). */
  start(onSamples: (mv: number[]) => void, onEnd: () => void): void
  close(): Promise<void>
}

export function serialSupported(): boolean {
  return 'serial' in navigator
}

/** Shows the browser's port picker. Rejects with NotFoundError if dismissed. */
export async function openSerialSource(): Promise<SampleSource> {
  const port = await navigator.serial.requestPort({ filters: [{ usbVendorId: SEGGER_VENDOR_ID }] })
  await port.open({ baudRate: BAUD_RATE, bufferSize: 1 << 16 })
  // The J-Link tri-states its UART lines until the host asserts DTR.
  await port.setSignals({ dataTerminalReady: true, requestToSend: true })

  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  let closing = false

  async function pump(onSamples: (mv: number[]) => void, onEnd: () => void) {
    const parser = new SampleParser()
    const decoder = new TextDecoder()
    let streamDone = false
    // A framing or overrun error fails the current stream but leaves
    // port.readable set, so read again; unplugging clears it.
    while (port.readable && !closing && !streamDone) {
      reader = port.readable.getReader()
      try {
        for (;;) {
          const { value, done } = await reader.read()
          if (done) {
            streamDone = true
            break
          }
          const samples = parser.push(decoder.decode(value, { stream: true }))
          if (samples.length) onSamples(samples)
        }
      } catch {
        // handled by the loop condition
      } finally {
        reader.releaseLock()
      }
    }
    if (!closing) {
      await port.close().catch(() => {})
      onEnd()
    }
  }

  return {
    start(onSamples, onEnd) {
      void pump(onSamples, onEnd)
    },
    async close() {
      closing = true
      await reader?.cancel().catch(() => {})
      await port.close().catch(() => {})
    },
  }
}
