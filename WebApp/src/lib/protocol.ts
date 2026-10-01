// The three-block cue protocol. A block is a list of back-to-back timed
// segments; the segment active when a sample arrives is that sample's label.

export interface Segment {
  /** Squeeze number within the block, 1-based. 0 during rest. */
  trial: number
  /** Instructed level: 0 = rest, 1-5 = squeeze. */
  target: number
  startMs: number
  endMs: number
}

export interface Block {
  id: number
  name: string
  instruction: string
  /** Block 1 squeezes are maximum voluntary contractions, shown as "Max" and labeled 5. */
  maxEffort: boolean
  segments: Segment[]
  durationMs: number
}

export const LEVELS = [1, 2, 3, 4, 5]
export const MVC_LEVEL = 5
export const RANDOM_CUE_COUNT = 18

type Step = { target: number; sec: number }

function toSegments(steps: Step[]): Segment[] {
  const segments: Segment[] = []
  let t = 0
  let trial = 0
  for (const { target, sec } of steps) {
    if (target > 0) trial += 1
    segments.push({ trial: target > 0 ? trial : 0, target, startMs: t, endMs: t + sec * 1000 })
    t += sec * 1000
  }
  return segments
}

function restThenHold(levels: number[], restSec: number, holdSec: number): Step[] {
  return levels.flatMap((level) => [
    { target: 0, sec: restSec },
    { target: level, sec: holdSec },
  ])
}

function shuffled<T>(items: T[], rng: () => number): T[] {
  const out = [...items]
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1))
    ;[out[i], out[j]] = [out[j], out[i]]
  }
  return out
}

/** `count` cues with every level appearing equally often (the remainder goes
 *  to randomly chosen distinct levels) and no level twice in a row. */
export function randomCues(count: number, rng: () => number = Math.random): number[] {
  const perLevel = Math.floor(count / LEVELS.length)
  const extras = shuffled(LEVELS, rng).slice(0, count % LEVELS.length)
  const pool = [...LEVELS.flatMap((level) => Array<number>(perLevel).fill(level)), ...extras]
  for (;;) {
    const cues = shuffled(pool, rng)
    if (cues.every((level, i) => i === 0 || level !== cues[i - 1])) return cues
  }
}

function block(id: number, name: string, instruction: string, steps: Step[], maxEffort = false): Block {
  const segments = toSegments(steps)
  return { id, name, instruction, maxEffort, segments, durationMs: segments[segments.length - 1].endMs }
}

export function buildBlocks(rng: () => number = Math.random): Block[] {
  return [
    block(
      1,
      'Calibration',
      'Relax your hand for 10 seconds. Then squeeze as hard as you can, twice, for 3 seconds each.',
      [
        { target: 0, sec: 10 },
        { target: MVC_LEVEL, sec: 3 },
        { target: 0, sec: 7 },
        { target: MVC_LEVEL, sec: 3 },
        { target: 0, sec: 7 },
      ],
      true,
    ),
    block(
      2,
      'Steady Holds',
      'Squeeze to the level shown and hold it steady for 3 seconds. Relax between squeezes.',
      restThenHold([...LEVELS, ...LEVELS, ...LEVELS], 4, 3),
    ),
    block(
      3,
      'Random Cues',
      'Levels now come in random order. Squeeze to each one for 2 seconds. Relax between squeezes.',
      restThenHold(randomCues(RANDOM_CUE_COUNT, rng), 4, 2),
    ),
  ]
}

/** The segment covering `elapsedMs`, or null once the block is over. */
export function segmentAt(segments: Segment[], elapsedMs: number): Segment | null {
  return segments.find((s) => elapsedMs >= s.startMs && elapsedMs < s.endMs) ?? null
}
