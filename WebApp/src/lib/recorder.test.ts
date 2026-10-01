import { describe, expect, it } from 'vitest'
import { CSV_HEADER, Recorder, fileName, safeId } from './recorder'

describe('Recorder', () => {
  it('writes a header and one labeled row per sample with a running count', () => {
    const recorder = new Recorder()
    recorder.add([-3.094, -3.069], 0.05, 1, 0, 0)
    recorder.add([0.5], 10.012, 1, 1, 5)
    recorder.add([-0.007], 31.4, 0, 0, 0)

    expect(recorder.length).toBe(4)
    expect(recorder.toCsv()).toBe(
      [
        CSV_HEADER,
        '-3.094,0,0.050,1,0,0',
        '-3.069,1,0.050,1,0,0',
        '0.500,2,10.012,1,1,5',
        '-0.007,3,31.400,0,0,0',
        '',
      ].join('\n'),
    )
  })

  it('keeps voltage and count as the first two columns, as emg_core.load_emg_csv expects', () => {
    expect(CSV_HEADER.split(',').slice(0, 2)).toEqual(['Voltage_mV', 'Count'])
  })
})

describe('fileName', () => {
  const started = new Date(2026, 9, 1, 14, 5, 9)

  it('combines participant and local start time', () => {
    expect(fileName('P01', started)).toBe('emg_P01_20261001_140509.csv')
  })

  it('marks partial recordings', () => {
    expect(fileName('P01', started, true)).toBe('emg_P01_20261001_140509_partial.csv')
  })

  it('strips characters that are unsafe in filenames', () => {
    expect(safeId('  Jane Doe / 7 ')).toBe('Jane_Doe_7')
    expect(safeId('   ')).toBe('')
  })
})
