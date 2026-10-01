// Demo mode's stand-in for the board. It emits the firmware's text format,
// log lines and awkward chunk boundaries included, so the real parser runs.
// The values are noise around a fixed offset and do not respond to the cues.

import { SampleParser, type SampleSource } from './serial'

const FS = 500
const TICK_MS = 50

export function createDemoSource(): SampleSource {
  let timer: ReturnType<typeof setInterval> | undefined

  return {
    start(onSamples) {
      const parser = new SampleParser()
      let carry = ''
      let ticks = 0
      timer = setInterval(() => {
        let text = carry
        for (let i = 0; i < (FS * TICK_MS) / 1000; i++) {
          const mv = -3 + (Math.random() - 0.5) * 0.2
          text += `>CH1:${mv.toFixed(3)}\r\n`
        }
        if (++ticks % 20 === 0) text += '[00:00:01.000,000] <inf> emg_software: Data sent\r\n'
        // Hold back the tail so lines straddle two chunks, as they do on a real port.
        carry = text.slice(-5)
        const samples = parser.push(text.slice(0, -5))
        if (samples.length) onSamples(samples)
      }, TICK_MS)
    },
    async close() {
      clearInterval(timer)
    },
  }
}
