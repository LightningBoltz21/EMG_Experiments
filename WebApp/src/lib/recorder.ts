// Accumulates labeled samples and writes the session CSV.
//
// The first two columns match the repo's existing recordings, so
// emg_core.load_emg_csv reads these files as-is and ignores the rest.

export const CSV_HEADER = 'Voltage_mV,Count,Time_s,Block,Trial,Target_Level'

export class Recorder {
  private rows: string[] = []

  get length(): number {
    return this.rows.length
  }

  /** `timeS` is when the batch arrived, so every sample in it shares it. */
  add(samples: number[], timeS: number, block: number, trial: number, target: number) {
    const tail = `${timeS.toFixed(3)},${block},${trial},${target}`
    for (const mv of samples) {
      this.rows.push(`${mv.toFixed(3)},${this.rows.length},${tail}`)
    }
  }

  toCsv(): string {
    return [CSV_HEADER, ...this.rows].join('\n') + '\n'
  }
}

/** Participant ID reduced to characters that are safe in a filename. */
export function safeId(participant: string): string {
  return participant.trim().replace(/[^A-Za-z0-9_-]+/g, '_')
}

export function fileName(participant: string, started: Date, partial = false): string {
  const p = (n: number) => String(n).padStart(2, '0')
  const date = `${started.getFullYear()}${p(started.getMonth() + 1)}${p(started.getDate())}`
  const time = `${p(started.getHours())}${p(started.getMinutes())}${p(started.getSeconds())}`
  return `emg_${safeId(participant)}_${date}_${time}${partial ? '_partial' : ''}.csv`
}

export function downloadCsv(name: string, csv: string) {
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  URL.revokeObjectURL(url)
}
