import { describe, expect, it } from 'vitest'
import { LEVELS, RANDOM_CUE_COUNT, buildBlocks, randomCues, segmentAt, type Block } from './protocol'

function seeded(seed: number): () => number {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const holds = (block: Block) => block.segments.filter((s) => s.target > 0)
const holdLevels = (block: Block) => holds(block).map((s) => s.target)
const holdSeconds = (block: Block) => holds(block).map((s) => (s.endMs - s.startMs) / 1000)

describe('buildBlocks', () => {
  const [calibration, plateau, random] = buildBlocks(seeded(1))

  it('block 1 is 10 s of rest then two 3 s maximum squeezes, 30 s in all', () => {
    expect(calibration.durationMs).toBe(30_000)
    expect(calibration.segments[0]).toMatchObject({ target: 0, startMs: 0, endMs: 10_000 })
    expect(holdLevels(calibration)).toEqual([5, 5])
    expect(holdSeconds(calibration)).toEqual([3, 3])
    expect(calibration.maxEffort).toBe(true)
  })

  it('block 2 is levels 1-5 three times, 3 s holds after 4 s rests', () => {
    expect(holdLevels(plateau)).toEqual([1, 2, 3, 4, 5, 1, 2, 3, 4, 5, 1, 2, 3, 4, 5])
    expect(new Set(holdSeconds(plateau))).toEqual(new Set([3]))
    expect(plateau.durationMs).toBe(15 * 7 * 1000)
    expect(plateau.segments[0]).toMatchObject({ target: 0, endMs: 4000 })
  })

  it('block 3 is 18 two-second cues', () => {
    expect(holds(random)).toHaveLength(RANDOM_CUE_COUNT)
    expect(new Set(holdSeconds(random))).toEqual(new Set([2]))
    expect(random.durationMs).toBe(RANDOM_CUE_COUNT * 6 * 1000)
  })

  it('segments are contiguous and trials count squeezes from 1', () => {
    for (const block of [calibration, plateau, random]) {
      block.segments.forEach((s, i) => {
        expect(s.startMs).toBe(i === 0 ? 0 : block.segments[i - 1].endMs)
        if (s.target === 0) expect(s.trial).toBe(0)
      })
      expect(holds(block).map((s) => s.trial)).toEqual(holds(block).map((_, i) => i + 1))
    }
  })
})

describe('randomCues', () => {
  it('never repeats a level back to back and uses every level at least 3 times', () => {
    for (let seed = 0; seed < 200; seed++) {
      const cues = randomCues(RANDOM_CUE_COUNT, seeded(seed))
      expect(cues).toHaveLength(RANDOM_CUE_COUNT)
      for (let i = 1; i < cues.length; i++) expect(cues[i]).not.toBe(cues[i - 1])
      for (const level of LEVELS) {
        const n = cues.filter((c) => c === level).length
        expect(n === 3 || n === 4).toBe(true)
      }
    }
  })

  it('differs between sessions', () => {
    expect(randomCues(RANDOM_CUE_COUNT, seeded(1))).not.toEqual(randomCues(RANDOM_CUE_COUNT, seeded(2)))
  })
})

describe('segmentAt', () => {
  const [calibration] = buildBlocks(seeded(1))

  it('labels by elapsed time, with the boundary belonging to the later segment', () => {
    expect(segmentAt(calibration.segments, 0)?.target).toBe(0)
    expect(segmentAt(calibration.segments, 9_999)?.target).toBe(0)
    expect(segmentAt(calibration.segments, 10_000)).toMatchObject({ target: 5, trial: 1 })
    expect(segmentAt(calibration.segments, 12_999)?.target).toBe(5)
    expect(segmentAt(calibration.segments, 13_000)?.target).toBe(0)
    expect(segmentAt(calibration.segments, 20_500)).toMatchObject({ target: 5, trial: 2 })
  })

  it('is null once the block has ended', () => {
    expect(segmentAt(calibration.segments, 30_000)).toBeNull()
  })
})
