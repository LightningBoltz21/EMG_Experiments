import { describe, expect, it } from 'vitest'
import { SOS, sosfilt_zi, sosfiltfilt } from './filter'

// The same input in NumPy:
//   n = np.arange(500)
//   x = -3.0 + 0.8*np.sin(2*np.pi*50*n/500) + 0.3*np.sin(2*np.pi*7*n/500) + 0.05*((n*37) % 11 - 5)
function testSignal(length: number): Float64Array {
  return Float64Array.from({ length }, (_, n) => {
    const tone = 0.8 * Math.sin((2 * Math.PI * 50 * n) / 500) + 0.3 * Math.sin((2 * Math.PI * 7 * n) / 500)
    return -3.0 + tone + 0.05 * (((n * 37) % 11) - 5)
  })
}

describe('sosfiltfilt', () => {
  // Expected values printed from scipy.signal.sosfiltfilt(sos, x) with SciPy 1.18.
  it('matches SciPy on the plotter-sized 500-sample window', () => {
    const y = sosfiltfilt(SOS, testSignal(500))
    expect(y).toHaveLength(500)
    expect(y[0]).toBeCloseTo(-0.012022209996888134, 9)
    expect(y[1]).toBeCloseTo(0.6240930924978125, 9)
    expect(y[13]).toBeCloseTo(0.9007939213915096, 9)
    expect(y[250]).toBeCloseTo(0.1978135486605791, 9)
    expect(y[498]).toBeCloseTo(-0.605489983205388, 9)
    expect(y[499]).toBeCloseTo(-0.018180282180642757, 9)
    expect(y.reduce((a, v) => a + v, 0)).toBeCloseTo(1.4470370855595396, 7)
    expect(y.reduce((a, v) => a + v * v, 0)).toBeCloseTo(169.62103384090537, 7)
  })

  it('matches SciPy on a short input, where the padding dominates', () => {
    const y = sosfiltfilt(SOS, testSignal(40))
    expect(y[0]).toBeCloseTo(-0.020691294615244826, 9)
    expect(y[20]).toBeCloseTo(-0.03898990489379045, 9)
    expect(y[39]).toBeCloseTo(-0.03803401917050429, 9)
  })

  it('removes the DC offset the electrodes sit at', () => {
    const y = sosfiltfilt(SOS, new Float64Array(500).fill(-3.094))
    expect(Math.max(...y.map(Math.abs))).toBeLessThan(1e-9)
  })

  it('refuses inputs no longer than its padding, as SciPy does', () => {
    expect(() => sosfiltfilt(SOS, new Float64Array(27))).toThrow(/padlen, which is 27/)
    expect(() => sosfiltfilt(SOS, new Float64Array(28))).not.toThrow()
  })
})

describe('sosfilt_zi', () => {
  it('gives sections after the first bandpass zero no step response to hold', () => {
    const zi = sosfilt_zi(SOS)
    expect(zi).toHaveLength(SOS.length)
    for (const z of [...zi[2], ...zi[3]]) expect(Math.abs(z)).toBe(0)
    expect(Math.abs(zi[1][0])).toBeGreaterThan(0)
  })
})
