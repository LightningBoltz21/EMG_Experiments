<script lang="ts">
  import { onMount } from 'svelte'
  import { buildBlocks, segmentAt, type Block } from './lib/protocol'
  import { Recorder, downloadCsv, fileName, safeId } from './lib/recorder'
  import { createDemoSource } from './lib/demo'
  import { EmgPlotter } from './lib/plotter'
  import { openSerialSource, serialSupported, type SampleSource } from './lib/serial'

  // Dev-only: ?speed=10 runs the session clock fast.
  const speed = import.meta.env.DEV ? Number(new URLSearchParams(location.search).get('speed')) || 1 : 1

  const supported = serialSupported()
  const NO_SIGNAL_MS = 2000
  const RING_RADIUS = 128
  const RING_LENGTH = 2 * Math.PI * RING_RADIUS

  type Screen = 'setup' | 'intro' | 'running' | 'done' | 'failed'
  type Saved = { name: string; csv: string; rows: number }

  let screen = $state<Screen>('setup')
  let participant = $state('')
  let connected = $state(false)
  let demo = $state(false)
  let hasSignal = $state(false)
  let noSignal = $state(false)
  let connectError = $state('')
  let failure = $state('')
  let saved = $state.raw<Saved | null>(null)
  let blocks = $state.raw<Block[]>([])
  let blockIndex = $state(0)
  let blockStart = $state(0)
  let now = $state(0)

  // Fed from the moment a source is attached, so the trace is already full of
  // real samples when a block starts.
  const plotter = new EmgPlotter()

  let source: SampleSource | null = null
  let recorder: Recorder | null = null
  let recordStart = 0
  let startedAt = new Date()
  let connectedAt = 0
  let lastSampleAt = 0

  // The always-visible indicator in the top-right corner.
  const link = $derived.by(() => {
    if (demo) return { text: 'Demo mode', tone: 'ok' }
    if (!connected) return { text: 'Wristband not connected', tone: 'off' }
    if (hasSignal) return { text: 'Wristband connected', tone: 'ok' }
    if (noSignal) return { text: 'No signal', tone: 'bad' }
    return { text: 'Connecting…', tone: 'off' }
  })

  const id = $derived(safeId(participant))
  const block = $derived(blocks[blockIndex])
  const eyebrow = $derived(block ? `${demo ? 'Demo · ' : ''}Block ${block.id} of ${blocks.length}` : '')
  const elapsed = $derived(screen === 'running' ? (now - blockStart) * speed : 0)
  const segment = $derived(block ? segmentAt(block.segments, elapsed) : null)
  const upcoming = $derived(segment ? block.segments[block.segments.indexOf(segment) + 1] : undefined)
  const squeezing = $derived(!!segment && segment.target > 0)
  const secondsLeft = $derived(segment ? Math.ceil((segment.endMs - elapsed) / 1000) : 0)
  const ringLeft = $derived(segment ? (segment.endMs - elapsed) / (segment.endMs - segment.startMs) : 0)

  function levelText(target: number): string {
    return block.maxEffort ? 'Max' : String(target)
  }

  // The instructed target at the moment a batch arrives is its label.
  function onSamples(mv: number[]) {
    const t = performance.now()
    lastSampleAt = t
    plotter.updatePlot(mv)
    if (!recorder) return
    const active = screen === 'running' ? segmentAt(block.segments, (t - blockStart) * speed) : null
    recorder.add(mv, (t - recordStart) / 1000, active ? block.id : 0, active?.trial ?? 0, active?.target ?? 0)
  }

  function onSourceEnd() {
    source = null
    connected = false
    if (recorder) fail('The wristband was disconnected.')
  }

  function attach(next: SampleSource) {
    source = next
    connectedAt = lastSampleAt = performance.now()
    connected = true
    source.start(onSamples, onSourceEnd)
  }

  async function detach() {
    await source?.close()
    source = null
    connected = false
  }

  async function connect() {
    connectError = ''
    try {
      attach(await openSerialSource())
    } catch (err) {
      // NotFoundError is the participant dismissing the port picker.
      if ((err as DOMException).name !== 'NotFoundError') {
        connectError = 'Could not open that port. Close any other program using it and try again.'
      }
    }
  }

  async function chooseAnotherPort() {
    await detach()
    await connect()
  }

  // Runs the whole session on mock data. The file is named as a demo so it
  // cannot be mistaken for a participant's recording.
  async function startDemo() {
    await detach()
    connectError = ''
    demo = true
    attach(createDemoSource(() => segment?.target ?? 0))
    begin()
  }

  // Recording runs from here to the end of the session, so there is always a
  // file to download, even if blocks are skipped. Rows outside a running
  // block are labeled Block 0.
  function begin() {
    blocks = buildBlocks()
    blockIndex = 0
    recorder = new Recorder()
    recordStart = performance.now()
    startedAt = new Date()
    screen = 'intro'
  }

  function startBlock() {
    blockStart = now = performance.now()
    screen = 'running'
  }

  // Also what "Skip Block" calls: samples already taken keep their labels,
  // and the skipped remainder is simply absent from the file.
  function endBlock() {
    if (blockIndex < blocks.length - 1) {
      blockIndex += 1
      screen = 'intro'
    } else {
      save(false)
      screen = 'done'
    }
  }

  function save(partial: boolean) {
    if (!recorder) return
    const who = demo ? 'demo' : participant
    saved = { name: fileName(who, startedAt, partial), csv: recorder.toCsv(), rows: recorder.length }
    recorder = null
    if (!partial) download()
  }

  function fail(reason: string) {
    save(true)
    failure = reason
    screen = 'failed'
  }

  function download() {
    if (saved) downloadCsv(saved.name, saved.csv)
  }

  async function reset() {
    if (demo) {
      await detach()
      demo = false
    }
    saved = null
    participant = ''
    screen = 'setup'
  }

  onMount(() => {
    let frame = requestAnimationFrame(function tick() {
      now = performance.now()
      const quietMs = now - lastSampleAt
      hasSignal = connected && quietMs < NO_SIGNAL_MS / 2
      noSignal = connected && quietMs > NO_SIGNAL_MS && now - connectedAt > NO_SIGNAL_MS
      if (recorder && noSignal) fail('The signal stopped.')
      else if (screen === 'running' && (now - blockStart) * speed >= block.durationMs) endBlock()
      frame = requestAnimationFrame(tick)
    })
    const warn = (event: BeforeUnloadEvent) => {
      if (recorder) event.preventDefault()
    }
    window.addEventListener('beforeunload', warn)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('beforeunload', warn)
    }
  })
</script>

<p class="link {link.tone}" role="status">{link.text}</p>

<main class="card">
  {#if screen === 'setup'}
    <div class="body">
      <h1>EMG Experiment</h1>
      <p>Three short blocks, about five minutes.</p>
      {#if supported}
        <label class="field">
          <span>Participant ID</span>
          <input bind:value={participant} placeholder="P01" autocomplete="off" spellcheck="false" />
        </label>
      {/if}
      {#if !supported}
        <p class="status bad">This browser can't connect to the wristband. Use desktop Chrome or Edge.</p>
      {:else if connectError}
        <p class="status bad">{connectError}</p>
      {:else if noSignal}
        <p class="status bad">No signal on this port. The board has more than one, so try another.</p>
      {/if}
    </div>
    <div class="actions">
      {#if !supported}
        <button class="primary" onclick={startDemo}>Try Demo</button>
      {:else if !connected}
        <button class="primary" onclick={connect}>Connect Wristband</button>
        <button class="plain" onclick={startDemo}>Try Demo</button>
      {:else if noSignal}
        <button class="primary" onclick={chooseAnotherPort}>Choose Another Port</button>
        <button class="plain" onclick={startDemo}>Try Demo</button>
      {:else}
        <button class="primary" disabled={!hasSignal || !id} onclick={begin}>Begin</button>
      {/if}
    </div>
  {:else if screen === 'intro'}
    <div class="body">
      <p class="eyebrow">{eyebrow}</p>
      <h1>{block.name}</h1>
      <p>{block.instruction}</p>
      <p class="duration">{block.durationMs < 60000 ? '30 seconds' : 'About 2 minutes'}</p>
    </div>
    <div class="actions">
      <button class="primary" onclick={startBlock}>Start</button>
      <button class="plain" onclick={endBlock}>Skip Block</button>
    </div>
  {:else if screen === 'running' && segment}
    <p class="eyebrow">{eyebrow}</p>
    <div class="cue" class:squeezing>
      <svg viewBox="0 0 280 280" aria-hidden="true">
        <circle class="track" cx="140" cy="140" r={RING_RADIUS} />
        <circle
          class="ring"
          cx="140"
          cy="140"
          r={RING_RADIUS}
          stroke-dasharray={RING_LENGTH}
          stroke-dashoffset={RING_LENGTH * (1 - ringLeft)}
        />
      </svg>
      <div class="target" aria-live="assertive">
        {#if squeezing}
          <span class={block.maxEffort ? 'word' : 'numeral'}>{levelText(segment.target)}</span>
        {:else}
          <span class="word">Rest</span>
        {/if}
        <span class="seconds">{secondsLeft}</span>
      </div>
    </div>
    <p class="hint">
      {#if squeezing}
        Squeeze and hold
      {:else if upcoming}
        Next: {block.maxEffort ? 'Max' : `Level ${upcoming.target}`}
      {:else}
        Almost done
      {/if}
    </p>
    <figure class="plot">
      <canvas use:plotter.attach aria-label="Live filtered EMG signal"></canvas>
      <figcaption><span>Voltage (mV)</span><span>Sample</span></figcaption>
    </figure>
    <div class="progress" aria-hidden="true">
      <div style:width="{(elapsed / block.durationMs) * 100}%"></div>
    </div>
    <button class="plain quiet" onclick={endBlock}>Skip Block</button>
  {:else if screen === 'done' && saved}
    <div class="body">
      <h1>{demo ? 'Demo Complete' : 'Session Complete'}</h1>
      <p>{demo ? 'This file holds mock data, not a real recording.' : 'Thank you. You can relax your hand.'}</p>
      <p class="file">{saved.name}</p>
    </div>
    <div class="actions">
      <button class="primary" onclick={download}>Download CSV</button>
      <button class="plain" onclick={reset}>{demo ? 'Exit Demo' : 'New Participant'}</button>
    </div>
  {:else if screen === 'failed'}
    <div class="body">
      <h1>Recording Stopped</h1>
      <p class="status bad">{failure}</p>
      {#if saved}
        <p>{saved.rows.toLocaleString()} samples were captured before it stopped.</p>
      {/if}
    </div>
    <div class="actions">
      {#if saved}
        <button class="primary" onclick={download}>Download Partial Recording</button>
      {/if}
      <button class="plain" onclick={reset}>Start Over</button>
    </div>
  {/if}
</main>

<style>
  .card {
    display: flex;
    flex-direction: column;
    align-items: center;
    width: min(460px, 100%);
    min-height: 620px;
    padding: 40px 32px 32px;
    border-radius: 32px;
    background: var(--card);
    text-align: center;
  }

  .body {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    width: 100%;
    margin-block: auto;
  }

  h1 {
    margin: 0;
    font-size: 36px;
    font-weight: 700;
    letter-spacing: -0.01em;
    line-height: 1.15;
  }

  p {
    margin: 0;
    color: var(--secondary);
    line-height: 1.4;
    text-wrap: balance;
  }

  .eyebrow {
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .duration,
  .file {
    font-size: 15px;
  }

  .file {
    color: var(--text);
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 8px;
    width: 100%;
    margin-top: 20px;
    color: var(--secondary);
    font-size: 13px;
    font-weight: 600;
    text-align: left;
  }

  input {
    height: 50px;
    padding: 0 16px;
    border: 0;
    border-radius: 14px;
    background: var(--fill);
    color: var(--text);
    font: inherit;
    font-size: 17px;
    font-weight: 400;
  }

  input::placeholder {
    color: var(--tertiary);
  }

  input:focus-visible,
  button:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--accent) 55%, transparent);
    outline-offset: 2px;
  }

  .status {
    font-size: 15px;
  }

  .link {
    position: fixed;
    top: 16px;
    right: 16px;
    padding: 8px 14px;
    border-radius: 999px;
    background: var(--card);
    font-size: 13px;
    font-weight: 600;
    line-height: 1.2;
  }

  .status::before,
  .link::before {
    content: '';
    display: inline-block;
    width: 8px;
    height: 8px;
    margin-right: 8px;
    border-radius: 50%;
    background: var(--tertiary);
  }

  .link.ok::before {
    background: var(--accent);
  }

  .status.bad,
  .link.bad {
    color: var(--danger);
  }

  .status.bad::before,
  .link.bad::before {
    background: var(--danger);
  }

  .actions {
    display: flex;
    flex-direction: column;
    gap: 4px;
    width: 100%;
  }

  button {
    min-height: 50px;
    border: 0;
    border-radius: 999px;
    font: inherit;
    font-weight: 600;
    cursor: pointer;
    transition:
      opacity 0.15s,
      transform 0.15s;
  }

  button:active {
    transform: scale(0.98);
  }

  .primary {
    background: var(--accent);
    color: var(--on-accent);
  }

  .primary:disabled {
    background: var(--fill);
    color: var(--tertiary);
    cursor: default;
    transform: none;
  }

  .plain {
    background: none;
    color: var(--accent);
  }

  /* Kept dim while cues are showing so it does not compete with the target. */
  .quiet {
    margin-top: 8px;
    margin-bottom: -16px;
    padding-inline: 20px;
    color: var(--secondary);
    font-size: 15px;
  }

  .cue {
    position: relative;
    width: 280px;
    height: 280px;
    margin-block: 20px 0;
  }

  svg {
    width: 100%;
    height: 100%;
    transform: rotate(-90deg);
  }

  circle {
    fill: none;
    stroke-width: 12;
  }

  .track {
    stroke: var(--fill);
  }

  .ring {
    stroke: var(--tertiary);
    stroke-linecap: round;
  }

  .squeezing .ring {
    stroke: var(--accent);
  }

  .target {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    color: var(--secondary);
  }

  .squeezing .target {
    color: var(--text);
  }

  .numeral {
    font-size: 132px;
    font-weight: 700;
    letter-spacing: -0.04em;
    line-height: 1;
  }

  .word {
    font-size: 60px;
    font-weight: 700;
    letter-spacing: -0.03em;
    line-height: 1.2;
  }

  .seconds {
    color: var(--secondary);
    font-size: 20px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .hint {
    margin-block: 24px;
    font-size: 20px;
    font-weight: 600;
  }

  .plot {
    width: 100%;
    margin: 0 0 20px;
  }

  canvas {
    display: block;
    width: 100%;
    height: 120px;
  }

  figcaption {
    display: flex;
    justify-content: space-between;
    padding-left: 34px;
    color: var(--tertiary);
    font-size: 11px;
    font-weight: 600;
  }

  .progress {
    width: 100%;
    height: 4px;
    border-radius: 2px;
    background: var(--fill);
    overflow: hidden;
  }

  .progress div {
    height: 100%;
    border-radius: 2px;
    background: var(--accent);
  }

  @media (prefers-reduced-motion: reduce) {
    button {
      transition: none;
    }

    button:active {
      transform: none;
    }
  }
</style>
