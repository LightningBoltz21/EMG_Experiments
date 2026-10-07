// The display filter from Firmware/monitor_visualization_scripts/filtered_plotter.py,
// ported from SciPy so the browser draws the same trace the Python plotter does.
// Function names follow scipy.signal. Used for the live plot only: the CSV stays raw.

export const SAMPLING_RATE_HZ = 500.0 // MATCHES THE ADS1298 CONFIG1 REGISTER
export const LOWCUT_HZ = 20.0
export const HIGHCUT_HZ = 200.0

type Sos = readonly (readonly number[])[]

// butter(4, [lowcut, highcut], btype='bandpass', fs=SAMPLING_RATE_HZ, output='sos'),
// printed from SciPy. Rows are [b0, b1, b2, a0, a1, a2]. Regenerate if any of the
// three constants above changes.
export const SOS: Sos = [
  [0.3023917920370139, 0.6047835840740278, 0.3023917920370139, 1.0, 0.998310234198152, 0.2763258189734928],
  [1.0, -2.0, 1.0, 1.0, -1.5507491526190176, 0.6091319098202675],
  [1.0, 2.0, 1.0, 1.0, 1.3300538838182236, 0.6515262413343961],
  [1.0, -2.0, 1.0, 1.0, -1.7769466016477833, 0.8358302801979054],
]

/** Cascade of biquads in transposed direct form II. `zi` holds two delays per
 *  section and is updated in place. */
export function sosfilt(sos: Sos, x: ArrayLike<number>, zi: number[][]): Float64Array {
  const y = Float64Array.from(x)
  sos.forEach(([b0, b1, b2, , a1, a2], section) => {
    let [z0, z1] = zi[section]
    for (let i = 0; i < y.length; i++) {
      const xi = y[i]
      const yi = b0 * xi + z0
      z0 = b1 * xi - a1 * yi + z1
      z1 = b2 * xi - a2 * yi
      y[i] = yi
    }
    zi[section] = [z0, z1]
  })
  return y
}

/** Delays that put each section in steady state for a unit step input. */
export function sosfilt_zi(sos: Sos): number[][] {
  let scale = 1.0
  return sos.map(([b0, b1, b2, , a1, a2]) => {
    const z0 = (b1 - a1 * b0 + (b2 - a2 * b0)) / (1 + a1 + a2)
    const z1 = (1 + a1) * z0 - (b1 - a1 * b0)
    const zi = [scale * z0, scale * z1]
    // Each section sees the step scaled by the DC gain of the ones before it.
    scale *= (b0 + b1 + b2) / (1 + a1 + a2)
    return zi
  })
}

/** Zero-phase filter: forward, then backward, over an odd-extended signal. */
export function sosfiltfilt(sos: Sos, x: ArrayLike<number>): Float64Array {
  let ntaps = 2 * sos.length + 1
  ntaps -= Math.min(sos.filter((s) => s[2] === 0).length, sos.filter((s) => s[5] === 0).length)
  const edge = 3 * ntaps
  const n = x.length
  if (n <= edge) throw new Error(`The length of the input must be greater than padlen, which is ${edge}.`)

  const ext = new Float64Array(n + 2 * edge)
  for (let i = 0; i < edge; i++) {
    ext[i] = 2 * x[0] - x[edge - i]
    ext[edge + n + i] = 2 * x[n - 1] - x[n - 2 - i]
  }
  for (let i = 0; i < n; i++) ext[edge + i] = x[i]

  const zi = sosfilt_zi(sos)
  const scaled = (by: number) => zi.map(([z0, z1]) => [z0 * by, z1 * by])
  const forward = sosfilt(sos, ext, scaled(ext[0]))
  forward.reverse()
  const backward = sosfilt(sos, forward, scaled(forward[0]))
  backward.reverse()
  return backward.slice(edge, edge + n)
}
