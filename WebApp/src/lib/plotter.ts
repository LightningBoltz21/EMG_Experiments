// The live trace, following MainWindow's plot path in
// Firmware/monitor_visualization_scripts/filtered_plotter.py: a ring buffer of
// the latest MAX_DATA_POINTS samples, zero-phase filtered as a whole window on
// every redraw. A canvas stands in for the pyqtgraph PlotWidget.

import { SOS, sosfiltfilt } from './filter'

export const MAX_DATA_POINTS = 500

// filtered_plotter.py fixes the axis at +/-20 mV and relies on its Scale box.
// With no controls here, the fixed range is sized to the recordings instead:
// filtered squeezes in Data_4 and Session_012 peak near 1.3 mV. It only ever
// grows, so a stronger signal is never clipped and levels stay comparable.
const Y_RANGE_MV = 2

const MARGIN = { left: 34, right: 6, top: 6, bottom: 18 }

export class EmgPlotter {
  /** Ring buffer for the live visual graph (keeps only the latest points). */
  private dataBuffer = new Float64Array(MAX_DATA_POINTS)
  private filterEnabled = true
  private yRange = Y_RANGE_MV
  private canvas: HTMLCanvasElement | null = null
  private frame = 0

  /** Svelte action: draws on `canvas` for as long as it is on screen. */
  attach = (canvas: HTMLCanvasElement) => {
    this.canvas = canvas
    this.redrawPlot()
    return {
      destroy: () => {
        cancelAnimationFrame(this.frame)
        this.frame = 0
        this.canvas = null
      },
    }
  }

  /** Update the visual screen buffer (pushes out old data), then redraw. */
  updatePlot(chunk: number[]) {
    const keep = Math.max(0, MAX_DATA_POINTS - chunk.length)
    this.dataBuffer.copyWithin(0, MAX_DATA_POINTS - keep)
    this.dataBuffer.set(chunk.slice(-MAX_DATA_POINTS), keep)
    // Serial batches arrive faster than the screen refreshes; draw once per frame.
    if (this.canvas && !this.frame) {
      this.frame = requestAnimationFrame(() => {
        this.frame = 0
        this.redrawPlot()
      })
    }
  }

  /** The window as it is drawn. */
  dataToPlot(): Float64Array {
    return this.filterEnabled ? sosfiltfilt(SOS, this.dataBuffer) : this.dataBuffer
  }

  /** Applies the zero-phase filter to the visual window and draws it. */
  private redrawPlot() {
    const canvas = this.canvas
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return
    const data = this.dataToPlot()

    const peak = data.reduce((max, v) => Math.max(max, Math.abs(v)), 0)
    if (peak > this.yRange) this.yRange = Math.ceil(peak)

    const dpr = window.devicePixelRatio || 1
    const width = canvas.clientWidth
    const height = canvas.clientHeight
    if (canvas.width !== width * dpr || canvas.height !== height * dpr) {
      canvas.width = width * dpr
      canvas.height = height * dpr
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, width, height)

    const style = getComputedStyle(canvas)
    const plotW = width - MARGIN.left - MARGIN.right
    const plotH = height - MARGIN.top - MARGIN.bottom
    const xOf = (i: number) => MARGIN.left + (i / (MAX_DATA_POINTS - 1)) * plotW
    const yOf = (mv: number) => MARGIN.top + ((this.yRange - mv) / (2 * this.yRange)) * plotH

    // showGrid(x=True, y=True)
    ctx.strokeStyle = style.getPropertyValue('--fill').trim()
    ctx.fillStyle = style.getPropertyValue('--secondary').trim()
    ctx.lineWidth = 1
    ctx.font = '10px system-ui, sans-serif'
    ctx.textBaseline = 'middle'
    ctx.textAlign = 'right'
    ctx.beginPath()
    for (const mv of [this.yRange, this.yRange / 2, 0, -this.yRange / 2, -this.yRange]) {
      ctx.moveTo(MARGIN.left, yOf(mv))
      ctx.lineTo(width - MARGIN.right, yOf(mv))
      if (mv === Math.round(mv)) ctx.fillText(String(mv), MARGIN.left - 6, yOf(mv))
    }
    ctx.textBaseline = 'top'
    for (let i = 0; i <= MAX_DATA_POINTS; i += 100) {
      const x = xOf(Math.min(i, MAX_DATA_POINTS - 1))
      ctx.moveTo(x, MARGIN.top)
      ctx.lineTo(x, MARGIN.top + plotH)
      ctx.textAlign = i === 0 ? 'left' : i === MAX_DATA_POINTS ? 'right' : 'center'
      ctx.fillText(String(i), x, MARGIN.top + plotH + 5)
    }
    ctx.stroke()

    // plot(pen=mkPen(width=2))
    ctx.strokeStyle = style.getPropertyValue('--accent').trim()
    ctx.lineWidth = 2
    ctx.lineJoin = 'round'
    ctx.beginPath()
    for (let i = 0; i < data.length; i++) ctx.lineTo(xOf(i), yOf(data[i]))
    ctx.stroke()
  }
}
