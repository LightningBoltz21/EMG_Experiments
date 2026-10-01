import { describe, expect, it } from 'vitest'
import { SampleParser, parseLine } from './serial'

describe('parseLine', () => {
  it('reads the firmware sample format', () => {
    expect(parseLine('>CH1:-3.094')).toBe(-3.094)
    expect(parseLine('>CH1:12.500')).toBe(12.5)
    expect(parseLine('>CH1:-0.007')).toBe(-0.007)
    expect(parseLine('>CH1:0.000')).toBe(0)
  })

  it('ignores Zephyr log output', () => {
    expect(parseLine('[00:00:00.412,000] <inf> emg_software: Starting Data Stream...')).toBeNull()
    expect(parseLine('*** Booting nRF Connect SDK v3.0.0 ***')).toBeNull()
    expect(parseLine('')).toBeNull()
  })

  it('rejects damaged lines instead of guessing a value', () => {
    expect(parseLine('>CH1:-3.0')).toBeNull()
    expect(parseLine('>CH1:-3.09>CH1:-3.094')).toBeNull()
    expect(parseLine('CH1:-3.094')).toBeNull()
    expect(parseLine('>CH1:')).toBeNull()
    expect(parseLine('>CH1:-3.094 [00:00:01.000,000] <inf>')).toBeNull()
  })
})

describe('SampleParser', () => {
  it('joins a line split across chunks', () => {
    const parser = new SampleParser()
    expect(parser.push('>CH1:-3.094\n>CH1:-3.0')).toEqual([-3.094])
    expect(parser.push('69\n>CH1:')).toEqual([-3.069])
    expect(parser.push('-3.058\n')).toEqual([-3.058])
  })

  it('accepts CRLF line endings and skips interleaved logs', () => {
    const parser = new SampleParser()
    const text = '>CH1:1.000\r\n[00:00:01.000,000] <inf> emg_software: Data sent\r\n>CH1:2.000\r\n'
    expect(parser.push(text)).toEqual([1, 2])
  })

  it('emits nothing until a line is complete', () => {
    const parser = new SampleParser()
    expect(parser.push('>CH1:1.000')).toEqual([])
    expect(parser.push('\n')).toEqual([1])
  })
})
