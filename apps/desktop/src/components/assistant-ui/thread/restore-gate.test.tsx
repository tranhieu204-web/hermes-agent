// The restore affordance is only offered on turns a rewind can actually reach.
//
// The transcript on screen is the display projection — it carries lineage from
// before a compaction handoff, which the gateway can render but cannot
// truncate. The gateway marks the reachable turns with `rewind_id`; a bubble
// without one used to still show the button, and clicking it could only ever
// produce "target user message is no longer in session history".
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Thread } from '.'

const createdAt = new Date('2026-05-01T00:00:00.000Z')

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', TestResizeObserver)
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
  window.setTimeout(() => callback(performance.now()), 0)
)
vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
vi.stubGlobal('CSS', { escape: (str: string) => str })

Element.prototype.scrollTo = function scrollTo() {}

function userMessage(id: string, text: string, rewindId?: null | string): ThreadMessage {
  return {
    id,
    role: 'user',
    content: [{ type: 'text', text }],
    attachments: [],
    createdAt,
    // `undefined` omits the key (legacy gateway); `null` sets it explicitly
    // (an id-capable gateway saying this turn is not rewindable).
    metadata: {
      custom: { attachmentRefs: [], ...(rewindId === undefined ? {} : { rewindId }) }
    }
  } as ThreadMessage
}

function assistantMessage(id: string, text: string): ThreadMessage {
  return {
    id,
    role: 'assistant',
    content: [{ type: 'text', text }],
    status: { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
  } as ThreadMessage
}

function Harness({ messages }: { messages: ThreadMessage[] }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages,
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread onRestoreToMessage={async () => {}} />
    </AssistantRuntimeProvider>
  )
}

const restoreButtons = () => screen.queryAllByRole('button', { name: 'Restore checkpoint' })

afterEach(() => {
  cleanup()
})

describe('restore button gating', () => {
  it('offers restore on a stamped turn', async () => {
    render(
      <Harness
        messages={[
          userMessage('u1', 'still in context', 'r2:0:deadbeefdeadbeefdeadbeef'),
          assistantMessage('a1', 'reply')
        ]}
      />
    )

    await screen.findByText('still in context')

    expect(restoreButtons()).toHaveLength(1)
  })

  it('hides restore when the gateway explicitly says the turn is not rewindable', async () => {
    // rewindId: null — an id-capable gateway ruling the turn out. Clicking here
    // could only 4018, or worse, cut a turn the user did not choose.
    render(<Harness messages={[userMessage('u1', 'compacted away', null), assistantMessage('a1', 'reply')]} />)

    await screen.findByText('compacted away')

    expect(restoreButtons()).toHaveLength(0)
  })

  it('keeps restore when the gateway is too old to have an opinion', async () => {
    // Key absent entirely. Gating on that would strip restore from every
    // message against an older backend, so the positional path stays.
    render(<Harness messages={[userMessage('u1', 'first'), assistantMessage('a1', 'reply')]} />)

    await screen.findByText('first')

    expect(restoreButtons()).toHaveLength(1)
  })

  it('does not infer capability from a thread where nothing is stamped', async () => {
    // The old probe treated "no row carries an id" as "old gateway" and showed
    // the button. A new gateway legitimately stamps nothing when every visible
    // turn is ancestor lineage — each row now carries its own explicit null.
    render(
      <Harness
        messages={[
          userMessage('u1', 'ancestor one', null),
          assistantMessage('a1', 'reply one'),
          userMessage('u2', 'ancestor two', null),
          assistantMessage('a2', 'reply two')
        ]}
      />
    )

    await screen.findByText('ancestor one')

    expect(restoreButtons()).toHaveLength(0)
  })
})
