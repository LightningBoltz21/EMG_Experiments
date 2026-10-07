// Demo mode's stand-in for the board. It emits the firmware's text format,
// log lines and awkward chunk boundaries included, so the real parser runs.
// The values are noise around a fixed offset that gets louder with the cued
// level, so the live plot has something to show. It is not a model of EMG.

import { SampleParser, type SampleSource } from './serial'

const FS = 500
const TICK_MS = 50
const OFFSET_MV = -3
const REST_NOISE_MV = 0.05
const NOISE_PER_LEVEL_MV = 0.18

/** `level` reports the target being cued right now: 0 for rest, 1-5 for a squeeze. */
export function createDemoSource(level: () => number): SampleSource {
  let timer: ReturnType<typeof setInterval> | undefined

  return {
    start(onSamples) {
      const parser = new SampleParser()
      let carry = ''
      let ticks = 0
      timer = setInterval(() => {
        const noise = REST_NOISE_MV + NOISE_PER_LEVEL_MV * level()
        let text = carry
        for (let i = 0; i < (FS * TICK_MS) / 1000; i++) {
          const mv = OFFSET_MV + (Math.random() - 0.5) * 2 * noise
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
